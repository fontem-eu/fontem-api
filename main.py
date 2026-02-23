from edgar import *
set_identity("your.name@example.com")

financials = Company("MSFT").get_financials()
print(financials.balance_sheet())   # Balance sheet with all line items
print(financials.income_statement())  # Revenue, net income, EPS


