from edgar import *

set_identity("Bernardo Marques bernardomarques5@gmail.com")

company_name = 'MSFT'
company_name = 'ASML'

facts = Company(company_name).get_facts().to_pandas()

facts_map = {
    'revenue': 'SalesRevenueNet',
    'income': 'NetIncomeLoss',
    'assets': 'Assets',
    'liabilities': 'Liabilities',
    'equity': 'StockholdersEquity',
    'shares': 'CommonStockSharesIssued',
    'shares_outstanding': 'CommonStockSharesOutstanding',
    'shares_issued': 'SharesIssued',
    'dividends': 'PaymentsOfDividends',
    'assets current': 'AssetsCurrent',
    'inventory': 'InventoryNet',
    'prepaid expenses': 'PrepaidExpenseCurrent',
    'liabilities current': 'LiabilitiesCurrent',
    'cash flow from operations': 'NetCashProvidedByUsedInContinuingOperations',
    'ppe': 'PropertyPlantAndEquipmentNet'
}

values = ['val', 'start', 'end', 'form', 'filed']

for fact in facts_map:
    print(fact)
    print(facts.query('fact == "%s"' % (facts_map[fact])).get(values))
    input()

