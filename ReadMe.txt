
# Roadmap

We should fetch stock market data (prices/volume/etc.)
https://github.com/mcdallas/wallstreet

Another option would be to use some high level pandas lib
https://github.com/davidastephens/pandas-finance
https://github.com/pydata/pandas-datareader

Or other packages:
https://github.com/theOGognf/finagg


Although the most comprehensive package seems to be:
https://github.com/JerBouma/FinanceDatabase
Note: This database is behind some levels of paywall... Not sure if great, perhaps some other options should be considered


We can perhaps use the FinanceDatabase for most of our needs and then use wallstreet (which should have less lag given that it relies on the
googlefinance API) for data with realtime requirements.


Would be nice to be able to calculate the intrinsic value of a stock, probably something inspired in this:
https://github.com/akashaero/Intrinsic-Value-Calculator


