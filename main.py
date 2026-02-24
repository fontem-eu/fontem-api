from edgar import *
from edgar.xbrl import XBRLS
set_identity("your.name@example.com")

company = Company("cuk")
#financials = company.get_financials()
#print(financials.balance_sheet())   # Balance sheet with all line items
#print(financials.income_statement())  # Revenue, net income, EPS

filings = company.get_filings(form="10-K").head(15)  # last x years
print(filings)

xbrls = XBRLS.from_filings(filings)

balance_sheet = xbrls.statements.balance_sheet(max_periods=None)
income = xbrls.statements.income_statement(max_periods=None)
cashflow = xbrls.statements.cashflow_statement(max_periods=None)

#balance_sheet = xbrls.statements.balance_sheet()
#income = xbrls.statements.income_statement()
#cashflow = xbrls.statements.cashflow_statement()

print(balance_sheet.to_dataframe())
print(income.to_dataframe())
print(cashflow.to_dataframe())

#print(balance_sheet)
#print(income)
#print(cashflow)

#print(len(xbrls))
