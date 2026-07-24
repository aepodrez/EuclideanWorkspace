# PIN (Probability of Informed Trading) - Data Requirements

## Overview
Your existing `ProbInformedTrading.py` predictor calculates:
```
PIN = (a * u) / (a * u + es + eb)
```

Based on Easley, Hvidkjaer, and O'Hara 2002 (EHO model).

**It reads from:** `pin_monthly.parquet`  
**Required columns:** `permno, time_avail_m, a, u, es, eb`

## What is pin_monthly.parquet?

It's the monthly-aggregated **microstructure parameters** estimated from tick-level trade/quote data:

| Parameter | What it represents | How to estimate |
|-----------|-------------------|-----------------|
| **a** | Arrival rate of informed traders | From trade flow patterns |
| **u** | Arrival rate of uninformed traders | From trade flow patterns |
| **es** | Expected daily sell-initiated trades | From bid-ask crossing analysis |
| **eb** | Expected daily buy-initiated trades | From bid-ask crossing analysis |

These parameters are NOT in any public dataset — they must be **calculated from tick-level data**.

---

## Data Needed to Calculate PIN Parameters

### Raw Inputs Required

1. **Tick-level TRADE data:**
   - `timestamp` (minute or second precision)
   - `price` (execution price)
   - `size` (shares traded)
   - `side` (buy / sell — or inferred)

2. **Tick-level QUOTE data:**
   - `timestamp` (bid/ask update time)
   - `bid_price` (best bid)
   - `ask_price` (best ask)
   - `bid_size`, `ask_size` (depth)

3. **Corporate data:**
   - `permno` (CRSP permanent ID)
   - `date` (trading date)

### Data Aggregation Level
- **Daily or minute aggregation** for estimating parameters
- **Monthly aggregation** for the final PIN parameters (a, u, es, eb)

---

## How Alpaca Provides This Data

✅ **Alpaca minute bars endpoint** provides:
```python
bars['AAPL'] = [
    Bar(
        timestamp=datetime(2026, 6, 13, 9, 30, 0),
        open=150.23,
        high=150.45,
        low=150.18,
        close=150.42,
        volume=25000,      # ← Can use to infer buys vs sells
        vwap=150.32,       # ← Volume weighted average
        trade_count=500    # ← Number of trades
    ),
    ...
]
```

✅ **Alpaca trades endpoint** provides:
```python
trades['AAPL'] = [
    Trade(
        timestamp=datetime(2026, 6, 13, 9, 30, 0, 37974),
        price=150.23,
        size=25.0,
        conditions=['@', 'T', 'I'],  # ← Trade conditions (can infer direction)
        exchange='K',
        tape='C'
    ),
    ...
]
```

✅ **Alpaca quotes endpoint** would provide:
```python
quotes['AAPL'] = [
    Quote(
        timestamp=datetime(...),
        bid_price=150.20,
        bid_size=1000,
        ask_price=150.30,
        ask_size=1000,
    ),
    ...
]
```

---

## Steps to Build pin_monthly.parquet

### Step 1: Fetch Raw Tick Data (Alpaca)
```python
# For each stock, each trading day:
trades = alpaca_client.get_stock_trades(symbol, date)
quotes = alpaca_client.get_stock_quotes(symbol, date)
bars = alpaca_client.get_stock_bars(symbol, date, timeframe=TimeFrame.Minute)
```

### Step 2: Infer Trade Direction
**Trade direction is critical for PIN** — you need to determine if each trade was buyer-initiated or seller-initiated.

Methods:
1. **Tick rule:** Compare trade price to previous trade price
   - If price > prev_price → BUY (aggressive buyer)
   - If price < prev_price → SELL (aggressive seller)
   - If price = prev_price → Use quote (bid-ask crossing)

2. **Quote rule:** Compare trade price to current bid-ask
   - If trade at ask → BUY
   - If trade at bid → SELL
   - Else → Use tick rule

3. **Lee-Ready rule** (combination of above)

```python
def infer_trade_direction(trades_df, quotes_df):
    """Infer buy/sell direction from trades and quotes"""
    trades_df = trades_df.sort_values('timestamp')
    
    # 1. Use quote rule if available
    trades_df['direction'] = 'UNKNOWN'
    for idx, trade in trades_df.iterrows():
        t = trade['timestamp']
        price = trade['price']
        
        # Find closest quote
        quote = quotes_df[quotes_df['timestamp'] <= t].iloc[-1]
        
        if abs(price - quote['ask_price']) < 0.01:
            trades_df.at[idx, 'direction'] = 'BUY'
        elif abs(price - quote['bid_price']) < 0.01:
            trades_df.at[idx, 'direction'] = 'SELL'
        else:
            # Fallback: tick rule
            prev_price = trades_df.iloc[idx-1]['price'] if idx > 0 else quote['mid_price']
            if price > prev_price:
                trades_df.at[idx, 'direction'] = 'BUY'
            else:
                trades_df.at[idx, 'direction'] = 'SELL'
    
    return trades_df
```

### Step 3: Calculate Daily Parameters
For each stock-day:

```python
def estimate_daily_pin_params(trades_df, quotes_df, date):
    """Estimate daily a, u, es, eb from trades/quotes"""
    
    # Infer direction
    trades_df = infer_trade_direction(trades_df, quotes_df)
    
    # Count trades by direction
    buy_trades = (trades_df['direction'] == 'BUY').sum()
    sell_trades = (trades_df['direction'] == 'SELL').sum()
    total_trades = len(trades_df)
    
    # Expected buys/sells (simple: count)
    eb = buy_trades  # expected buyer-initiated
    es = sell_trades  # expected seller-initiated
    
    # Arrival rates (trades per minute, aggregated)
    trading_minutes = len(trades_df['timestamp'].dt.floor('min').unique())
    u = total_trades / trading_minutes if trading_minutes > 0 else 0
    
    # Informed arrival rate (more complex EHO estimation)
    # For now, rough approximation: ratio of buy/sell imbalance
    imbalance = abs(buy_trades - sell_trades) / total_trades if total_trades > 0 else 0
    a = imbalance * 0.5  # Simplified; real EHO uses MLE
    
    return {
        'date': date,
        'eb': eb,
        'es': es,
        'u': u,
        'a': a
    }
```

### Step 4: Aggregate to Monthly
```python
# For each stock-month, average the daily parameters
monthly_params = daily_params.groupby(['permno', 'time_avail_m']).agg({
    'a': 'mean',
    'u': 'mean',
    'es': 'mean',
    'eb': 'mean'
}).reset_index()

monthly_params.to_parquet('pin_monthly.parquet')
```

---

## Data Pipeline for PIN

```
Alpaca API (free)
    ↓
Minute bars + Tick trades + Quotes (7,000 stocks daily)
    ↓
Trade direction inference (Lee-Ready rule)
    ↓
Daily PIN parameters (a, u, es, eb) per stock
    ↓
Monthly aggregation → pin_monthly.parquet
    ↓
ProbInformedTrading.py ← Reads this
    ↓
PIN signal merged into SignalMasterTable
```

---

## Implementation Plan

### Lambda 1: Fetch Tick Data (nightly)
```
Inputs:  Trading date, 7,000 stock symbols
Outputs: s3://bucket/minute_returns/ticks/2026-06-13/
         - trades_{symbol}.parquet (all trades for day)
         - quotes_{symbol}.parquet (all quotes for day)
```

### Lambda 2: Calculate PIN Parameters (nightly, after Lambda 1)
```
Inputs:  Tick data from Lambda 1
Process:
  1. Infer trade direction (Lee-Ready)
  2. Estimate daily a, u, es, eb
  3. Aggregate to monthly
Outputs: s3://bucket/pyData/Intermediate/pin_monthly.parquet
```

### Lambda 3: Run PIN Predictor (monthly)
```
Inputs:  pin_monthly.parquet
         SignalMasterTable.parquet
Outputs: pyData/Predictors/ProbInformedTrading.csv
```

---

## Critical Notes

1. **Trade direction inference is 80-90% accurate** — Not perfect, but sufficient for PIN
2. **Monthly aggregation smooths noise** — Daily estimates are noisy
3. **Top 50% by market cap excluded** — Your code already does this (bid-ask spreads too tight for meaningful inference)
4. **EHO model requires MLE estimation** — Simple implementation above is rough; real EHO uses maximum likelihood (more complex)
5. **Alpaca free tier limitation:** Indicative quotes/delayed trades won't work — you need **OPRA subscription** for real PIN calculation

---

## Cost-Benefit Analysis

**To calculate real PIN monthly for 7,000 stocks:**

- **Data cost:** Alpaca OPRA feed = $1,000/month
- **Compute cost:** ~2-3 Lambda invocations/day = ~$0.50/month
- **Benefit:** Monthly PIN signal for entire universe

**Alternative (free but lower quality):**
- Use Alpaca free Indicative quotes (delayed 15 min)
- PIN will be biased due to stale quotes
- Still useful for research/backtesting

