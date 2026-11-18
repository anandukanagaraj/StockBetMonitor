from services.number_format import (
    format_indian_compact_money,
    format_indian_decimal,
    format_indian_inr,
    format_indian_money,
    format_indian_money_signed,
    format_indian_number,
)


def test_indian_lakh_format():
    assert format_indian_number(1000055.48) == "10,00,055.48"
    assert format_indian_money(1000055.48) == "₹10,00,055.48"


def test_indian_thousand_format():
    assert format_indian_number(1000) == "1,000.00"
    assert format_indian_money(1000) == "₹1,000.00"


def test_indian_crore_format():
    assert format_indian_number(12345678.9) == "1,23,45,678.90"


def test_negative_indian_money():
    assert format_indian_money(-1000055.48) == "-₹10,00,055.48"
    assert format_indian_money_signed(-1000055.48) == "-₹10,00,055.48"


def test_signed_positive_money():
    assert format_indian_money_signed(500) == "+₹500.00"


def test_inr_prefix():
    assert format_indian_inr(1000055.48) == "INR 10,00,055.48"


def test_integer_decimal_format():
    assert format_indian_decimal(100000) == "1,00,000"


def test_compact_money_lakhs():
    assert format_indian_compact_money(942890) == "₹9.43L"


def test_compact_money_crores():
    assert format_indian_compact_money(12500000) == "₹1.25Cr"


def test_compact_money_thousands():
    assert format_indian_compact_money(4500) == "₹4.50k"


def test_compact_money_small_value():
    assert format_indian_compact_money(850) == "₹850.00"
