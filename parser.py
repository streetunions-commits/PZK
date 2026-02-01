import re
from dataclasses import dataclass, field
from typing import Optional

import pdfplumber


@dataclass
class StatementHeader:
    document_number: str = ""
    document_date: str = ""
    owner: str = ""
    account_number: str = ""
    account_opened: str = ""
    generated_at: str = ""
    period_from: str = ""
    period_to: str = ""
    currency: str = ""
    opening_balance: float = 0.0


@dataclass
class Transaction:
    date: str
    time: str
    document: str
    description: str
    amount: float
    is_credit: bool  # True = зачисление, False = списание


@dataclass
class StatementFooter:
    total_credits: float = 0.0
    total_debits: float = 0.0
    closing_balance: float = 0.0


@dataclass
class BankStatement:
    header: StatementHeader = field(default_factory=StatementHeader)
    transactions: list[Transaction] = field(default_factory=list)
    footer: StatementFooter = field(default_factory=StatementFooter)


def parse_amount(text: str) -> tuple[float, bool]:
    """Парсит сумму вида '+ 6 812.98 ₽' или '- 20 000.00 ₽'. Возвращает (сумма, is_credit)."""
    text = text.strip()
    is_credit = text.startswith("+")
    cleaned = re.sub(r"[+\-₽\s]", "", text).replace("\u00a0", "").strip()
    cleaned = cleaned.replace(",", ".")
    try:
        amount = float(cleaned)
    except ValueError:
        amount = 0.0
    return amount, is_credit


def parse_header_text(full_text: str) -> StatementHeader:
    """Извлекает метаданные из текстового содержимого PDF."""
    header = StatementHeader()

    m = re.search(r"№\s*(Ф-[\d\-]+)", full_text)
    if m:
        header.document_number = m.group(1)

    m = re.search(r"от\s*[«\"]\s*(\d{1,2})\s*[»\"]\s*(\S+)\s*(\d{4})\s*года", full_text)
    if m:
        header.document_date = f"{m.group(1)} {m.group(2)} {m.group(3)}"

    m = re.search(r"Владелец:\s*\n?\s*(.+)", full_text)
    if m:
        header.owner = m.group(1).strip()

    m = re.search(r"№\s*(409\d+)", full_text)
    if m:
        header.account_number = m.group(1)

    m = re.search(r"открыт\s*([\d.]+)", full_text)
    if m:
        header.account_opened = m.group(1)

    m = re.search(r"формирования документа:\s*([\d.\s:]+)", full_text)
    if m:
        header.generated_at = m.group(1).strip()

    m = re.search(r"Период выписки:\s*([\d.]+)\s*[–\-]\s*([\d.]+)", full_text)
    if m:
        header.period_from = m.group(1)
        header.period_to = m.group(2)

    m = re.search(r"Валюта:\s*(.+)", full_text)
    if m:
        header.currency = m.group(1).strip()

    m = re.search(r"Входящий остаток:\s*([\d\s,.]+)", full_text)
    if m:
        val = m.group(1).replace("\u00a0", "").replace(" ", "").replace(",", ".")
        try:
            header.opening_balance = float(val)
        except ValueError:
            pass

    return header


def parse_footer_text(full_text: str) -> StatementFooter:
    """Извлекает итоги из текстового содержимого PDF."""
    footer = StatementFooter()

    def extract_total(pattern: str) -> float:
        m = re.search(pattern, full_text)
        if m:
            val = m.group(1).replace("\u00a0", "").replace(" ", "").replace(",", ".")
            try:
                return float(val)
            except ValueError:
                pass
        return 0.0

    footer.total_credits = extract_total(r"Итого зачислений за период:\s*([\d\s,.]+)")
    footer.total_debits = extract_total(r"Итого списаний за период:\s*([\d\s,.]+)")
    footer.closing_balance = extract_total(r"Исходящий остаток:\s*([\d\s,.]+)")

    return footer


def parse_transactions_from_tables(pdf: pdfplumber.PDF) -> list[Transaction]:
    """Извлекает транзакции из таблиц PDF."""
    transactions = []

    for page in pdf.pages:
        tables = page.extract_tables()
        for table in tables:
            for row in table:
                if not row or len(row) < 4:
                    continue

                # Пропускаем заголовки таблицы
                cell0 = (row[0] or "").strip()
                if cell0 in ("Дата операции", "") or "Сумма операции" in cell0:
                    continue

                # Проверяем, что первая ячейка содержит дату
                date_match = re.match(r"(\d{2}\.\d{2}\.\d{4})\s*(\d{2}:\d{2}:\d{2})?", cell0)
                if not date_match:
                    continue

                date_str = date_match.group(1)
                time_str = date_match.group(2) or ""

                document = (row[1] or "").strip()
                description = (row[2] or "").strip().replace("\n", " ")

                # Сумма в рублях — обычно 4-й столбец (индекс 3)
                amount_text = (row[3] or "").strip()
                if not amount_text:
                    # Иногда столбцы сдвинуты
                    amount_text = (row[-2] or "").strip() if len(row) >= 5 else ""

                if not amount_text:
                    continue

                amount, is_credit = parse_amount(amount_text)

                transactions.append(Transaction(
                    date=date_str,
                    time=time_str,
                    document=document,
                    description=description,
                    amount=amount,
                    is_credit=is_credit,
                ))

    return transactions


def parse_ozon_bank_pdf(file_path: str) -> BankStatement:
    """Основная функция парсинга PDF выписки Озон Банка."""
    statement = BankStatement()

    with pdfplumber.open(file_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        statement.header = parse_header_text(full_text)
        statement.footer = parse_footer_text(full_text)
        statement.transactions = parse_transactions_from_tables(pdf)

    return statement
