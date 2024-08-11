from edgar import *
from wallstreet import Stock

company_name = 'MSFT'
company_name = 'ASML'

def load_edgar_data():

    set_identity("Bernardo Marques bernardomarques5@gmail.com")

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

def deprecated_get_historic_stock_data():
    import financedatabase as findb
    equities = findb.Equities()
    # company = equities.search(name="ASML Holding N.V.", currency="EUR", exchange='GER')
    # company = equities.search(index='ASME.DE')
    company = equities.search(index='^%s$' % (company_name))
    company_tk = company.to_toolkit()
    historical_data = company_tk.get_historical_data()

    symbol = company.index[0]

    fields = [
        'Open',
        'High',
        'Low',
        'Close',
        'Adj Close',
        'Volume',
        'Dividends',
        'Return',
        'Volatility',
        'Excess Return',
        'Excess Volatility',
        'Cumulative Return',
    ]

    return historical_data.get([(field,symbol) for field in fields])

def get_historical_stock_data():
    s = Stock(company_name)
    return s.historical(days_back=365 * 50, frequency='d')

def get_current_stock_price():
    s = Stock(company_name)
    return s.price

def get_splits():
    import yfinance as yf
    company = yf.Ticker(company_name)
    return company.actions.query('`Stock Splits` > 0').get('Stock Splits')

if __name__ == '__main__':

    # print(get_historical_stock_data())
    print(get_splits())
