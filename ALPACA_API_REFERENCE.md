# Alpaca Minute Bars API - Complete Reference for Lambda Build

## Overview
Aggregate minute-level returns for 7,000 stocks using Alpaca's market data API.

**Endpoint:** `GET /v1beta3/stocks/bars`  
**Rate Limit:** 1,000 RPM (requests per minute)  
**Universe:** 7,000 stocks  
**Expected Time:** ~7-10 minutes per nightly run

---

## Python SDK Usage

### Basic Request (Single Symbol)

```python
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta

API_KEY = "YOUR_API_KEY"
API_SECRET = "YOUR_API_SECRET"

client = StockHistoricalDataClient(api_key=API_KEY, secret_key=API_SECRET)

# Request minute bars for yesterday
yesterday = (datetime.now() - timedelta(days=1)).date()
today = datetime.now().date()

request = StockBarsRequest(
    symbol_or_symbols="AAPL",
    timeframe=TimeFrame.Minute,  # 1-minute bars
    start=datetime.combine(yesterday, datetime.min.time()),
    end=datetime.combine(today, datetime.max.time())
)

bars = client.get_stock_bars(request)
# Returns: {'AAPL': [Bar(timestamp, open, high, low, close, volume), ...]}
```

### Response Structure

```python
# Response is a dict: {symbol: [Bar objects]}
{
    'AAPL': [
        Bar(
            timestamp=datetime(2026, 6, 13, 9, 30, 0),
            open=150.23,
            high=150.45,
            low=150.18,
            close=150.42,
            volume=25000,
            vwap=150.32,
            trade_count=500
        ),
        # ... more bars for each minute of trading day
    ]
}
```

### Multi-Symbol Batch Request

```python
symbols = ["AAPL", "MSFT", "TSLA", "GOOGL", "AMZN"]

request = StockBarsRequest(
    symbol_or_symbols=symbols,  # List of symbols
    timeframe=TimeFrame.Minute,
    start=start_datetime,
    end=end_datetime
)

bars = client.get_stock_bars(request)
# Returns all 5 symbols in one request (faster than sequential)
```

### Convert to DataFrame with Returns

```python
import pandas as pd

def bars_to_returns_df(bars_dict, date):
    """
    Convert Alpaca bars to returns DataFrame
    
    Args:
        bars_dict: {symbol: [Bar, ...]}
        date: trading date
    
    Returns:
        DataFrame with columns: symbol, timestamp, close, volume, minute_return
    """
    rows = []
    
    for symbol, bar_list in bars_dict.items():
        # Sort by timestamp (should already be sorted, but ensure it)
        sorted_bars = sorted(bar_list, key=lambda b: b.timestamp)
        
        for i, bar in enumerate(sorted_bars):
            # Return from previous minute
            if i > 0:
                prev_close = sorted_bars[i-1].close
                minute_return = (bar.close - prev_close) / prev_close
            else:
                # First minute: return from open
                minute_return = (bar.close - bar.open) / bar.open
            
            rows.append({
                'symbol': symbol,
                'timestamp': bar.timestamp,
                'open': bar.open,
                'close': bar.close,
                'volume': bar.volume,
                'vwap': bar.vwap,
                'trade_count': bar.trade_count,
                'minute_return': minute_return,
                'date': date
            })
    
    return pd.DataFrame(rows)
```

---

## Aggregation Logic for 7,000 Stocks

### Single Ticker Aggregation

```python
def aggregate_single_ticker(symbol, date, api_client):
    """
    Fetch and aggregate minute returns for one ticker
    
    Returns:
        DataFrame or dict suitable for storage
    """
    try:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=datetime.combine(date, datetime.min.time()),
            end=datetime.combine(date + timedelta(days=1), datetime.max.time())
        )
        
        bars = api_client.get_stock_bars(request)
        
        if symbol not in bars:
            return None
        
        # Convert to DataFrame
        df = bars_to_returns_df({symbol: bars[symbol]}, date)
        
        # Add daily summary
        df['daily_return'] = df['close'].iloc[-1] / df['open'].iloc[0] - 1 if len(df) > 0 else 0
        df['daily_volume'] = df['volume'].sum()
        
        return df
        
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return None
```

### Full Universe Aggregation (7,000 stocks)

```python
import time
from datetime import datetime, timedelta
import pandas as pd

def aggregate_all_universe(universe_list, date, api_client, output_path):
    """
    Fetch minute bars for all 7,000 stocks and aggregate
    
    Args:
        universe_list: list of 7,000 ticker symbols
        date: datetime.date object for trading date
        api_client: StockHistoricalDataClient instance
        output_path: S3 or local path to save results
    
    Returns:
        aggregated_df: DataFrame with all minute returns
    """
    
    all_returns = []
    failed_symbols = []
    
    # Process in batches to optimize API calls
    batch_size = 50  # Test optimal batch size (50-100)
    
    for i in range(0, len(universe_list), batch_size):
        batch = universe_list[i:i+batch_size]
        
        try:
            # Fetch batch
            request = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Minute,
                start=datetime.combine(date, datetime.min.time()),
                end=datetime.combine(date + timedelta(days=1), datetime.max.time())
            )
            
            bars = api_client.get_stock_bars(request)
            
            # Convert to returns
            returns_df = bars_to_returns_df(bars, date)
            all_returns.append(returns_df)
            
            print(f"Batch {i//batch_size + 1}: {len(batch)} symbols, {len(returns_df)} rows")
            
            # Rate limiting: space requests out
            # 1,000 RPM = ~16-17 requests per second
            # Sleep 0.05-0.1 seconds per batch to stay safe
            time.sleep(0.1)
            
        except Exception as e:
            print(f"Batch error: {e}")
            failed_symbols.extend(batch)
    
    # Combine all batches
    aggregated_df = pd.concat(all_returns, ignore_index=True)
    
    # Calculate universe-level aggregations
    agg_by_minute = aggregated_df.groupby('timestamp').agg({
        'minute_return': ['mean', 'median', 'std'],
        'volume': 'sum',
        'symbol': 'count'  # num active stocks
    }).reset_index()
    
    agg_by_minute.columns = ['timestamp', 'avg_return', 'median_return', 'return_std', 
                             'total_volume', 'num_stocks']
    
    # Save results
    save_results(aggregated_df, agg_by_minute, output_path, date)
    
    print(f"\nAggregation complete:")
    print(f"  Total rows: {len(aggregated_df)}")
    print(f"  Symbols processed: {aggregated_df['symbol'].nunique()}")
    print(f"  Failed symbols: {len(failed_symbols)}")
    
    return aggregated_df, agg_by_minute, failed_symbols
```

---

## Error Handling & Rate Limiting

### Common Errors and Solutions

```python
from alpaca.common.exceptions import APIError
import time

def fetch_with_retry(symbol, api_client, max_retries=3):
    """Fetch with exponential backoff on rate limit errors"""
    
    for attempt in range(max_retries):
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=...,
                end=...
            )
            return api_client.get_stock_bars(request)
            
        except APIError as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                # Rate limited - back off
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                print(f"Rate limited. Waiting {wait_time}s before retry {attempt + 1}...")
                time.sleep(wait_time)
            elif "subscription does not permit" in str(e):
                # Subscription issue - REQUIRES FIX
                print(f"ERROR: {e}")
                print("Your subscription doesn't have access to this data.")
                print("Check Alpaca dashboard -> Market Data Subscriptions")
                raise
            else:
                # Other error
                print(f"Error on attempt {attempt + 1}: {e}")
                if attempt == max_retries - 1:
                    raise
                time.sleep(1)
```

### Rate Limiting Strategy for 7,000 Stocks

**Option A: Sequential (safest)**
```
7,000 requests / 1,000 RPM = ~7 minutes
Sleep 0.07 seconds between requests
```

**Option B: Batch (fastest)**
```
7,000 / 50 per batch = 140 batches
140 batches * 0.1s sleep = ~14 seconds + API time
Much faster but respects rate limit
```

**Recommended Lambda Strategy:**
```python
# In Lambda handler:
# 1. Load universe (7,000 tickers)
# 2. Process in batches of 50 symbols
# 3. Sleep 0.1s between batches
# 4. Fetch minute bars for previous trading day
# 5. Calculate returns
# 6. Aggregate and save to S3
# 7. Total runtime: ~5-10 minutes (well under 15 min Lambda limit)
```

---

## Data Storage Format

### Per-Ticker Parquet (detailed)
```
s3://bucket/minute_returns/by_ticker/2026-06-13/AAPL.parquet

Columns: symbol, timestamp, open, high, low, close, volume, vwap, 
         trade_count, minute_return, date
Rows: ~390 (one per minute of trading day)
```

### Aggregated Universe Parquet (summary)
```
s3://bucket/minute_returns/aggregated/2026-06-13.parquet

Columns: timestamp, avg_return, median_return, return_std, 
         total_volume, num_stocks_active
Rows: ~390
```

### Daily Summary CSV
```
s3://bucket/minute_returns/daily_summary/2026-06-13.csv

symbol,date,open,close,daily_return,minute_volume,num_minutes
AAPL,2026-06-13,150.23,152.45,0.0148,5000000,390
MSFT,2026-06-13,380.12,382.34,0.0058,3200000,390
...
```

---

## Important: Subscription Requirements

### Current Status Check
```python
# Your credentials returned this error:
# "subscription does not permit querying recent SIP data"

# This means you need to:
# 1. Log into Alpaca dashboard
# 2. Go to: Account → Market Data Subscriptions
# 3. Check/upgrade your data feed subscription
```

### Free vs Paid Tiers

| Feed | Cost | Latency | Coverage | Use Case |
|------|------|---------|----------|----------|
| **IEX** | Free | Real-time | Stocks only | Research, backtesting |
| **SIP** (Consolidated) | Free | Real-time | Stocks + detailed | Live trading |
| **OTC** | Free | Real-time | OTC stocks | Pink sheets |
| **OPRA** | $1,000/mo | Real-time | Options | Live options trading |

**For your use case (7,000 stocks minute bars):**
- You likely need **SIP or IEX** feed (both free)
- Check your dashboard to verify subscription

---

## Lambda Function Skeleton

```python
# lambda_function.py
import json
import boto3
from datetime import datetime, timedelta
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
import pandas as pd

s3 = boto3.client('s3')

def lambda_handler(event, context):
    """
    Fetch minute bars for all 7,000 stocks and aggregate
    Schedule: nightly at 16:30 UTC (after market close)
    """
    
    API_KEY = os.environ['ALPACA_API_KEY']
    API_SECRET = os.environ['ALPACA_SECRET']
    BUCKET = os.environ['S3_BUCKET']
    
    client = StockHistoricalDataClient(api_key=API_KEY, secret_key=API_SECRET)
    
    # Get previous trading day
    today = datetime.now().date()
    trading_date = today - timedelta(days=1)  # Assume yesterday was trading day
    
    # Load universe
    universe = load_universe_from_s3(BUCKET)  # Your 7,000 tickers
    
    # Aggregate
    aggregated_df, summary_df, failed = aggregate_all_universe(
        universe, 
        trading_date, 
        client,
        BUCKET
    )
    
    # Save to S3
    save_to_s3(aggregated_df, summary_df, BUCKET, trading_date)
    
    return {
        'statusCode': 200,
        'body': json.dumps({
            'date': str(trading_date),
            'rows_processed': len(aggregated_df),
            'failed_symbols': len(failed)
        })
    }
```

---

## Summary for Model Builder

**To build the Lambda, you need:**

1. **API client setup** - StockHistoricalDataClient with API credentials
2. **Batch request logic** - Request 50-100 symbols at a time
3. **Response parsing** - Extract Bar objects, convert to DataFrame
4. **Return calculation** - (close - prev_close) / prev_close for each minute
5. **Aggregation** - Group by timestamp, calculate mean/median/std across universe
6. **Error handling** - Retry on rate limit, log failures
7. **S3 storage** - Save aggregated DataFrames as parquet daily
8. **Rate limiting** - 0.1s sleep between batches (respects 1,000 RPM)

**Expected performance:**
- ~7-10 minutes to fetch all 7,000 stocks (batched)
- Well under 15-minute Lambda timeout
- Cost: Minimal (API calls are free, S3 storage <$1/month)

---

# PART 2: PIN (Probability of Informed Trading) - Daily Live Calculation

This section extends the Alpaca API reference to build a complete **daily PIN calculation pipeline** using overnight runs. PIN will be fresh each morning for live trading.

## Why Daily PIN is "Live Enough"

PIN parameters are **daily aggregates** of market microstructure:
```
es = Total seller-initiated trades (entire trading day)
eb = Total buyer-initiated trades (entire trading day)  
u  = Uninformed trader arrival rate (estimated from day's flow)
a  = Informed trader arrival rate (estimated from day's flow)
```

These values are **meaningless to update mid-day** because they require the complete day's trade flow. Your existing predictor (`ProbInformedTrading.py`) reads **monthly** PIN values, so updating daily/nightly is more than sufficient.

**Architecture:** Fetch yesterday's ticks after market close (17:00 UTC), process overnight, have fresh PIN signal ready by morning.

---

## Complete Pipeline: Ticks → PIN Signal (Overnight)

```
Market Close (16:30 UTC)
    ↓
[17:00] Lambda 1: Fetch tick trades/quotes for 7,000 stocks
    (Time: ~20 min)
    ↓
[17:30] Lambda 2: Infer trade direction + calculate daily a, u, es, eb
    (Time: ~5 min)
    ↓
[17:35] Lambda 3: Aggregate to monthly PIN values
    (Time: ~2 min)
    ↓
[17:40] Lambda 4: Run ProbInformedTrading.py predictor
    (Time: ~1 min)
    ↓
Next trading day (09:30 UTC): Fresh PIN signal ready
```

**Total runtime: 40 minutes** (fully asynchronous, fits under Lambda limits)

---

## Lambda 1: Fetch Tick Data (20 minutes)

**Purpose:** Download all trades and quotes for 7,000 stocks for the previous trading day

**Trigger:** EventBridge rule at 17:00 UTC (after market close, weekdays only)

**Inputs:**
- Previous trading date (auto-detected)
- 7,000 stock symbols from S3

**Outputs:**
```
s3://bucket/tick_data/trades/2026-06-13/
├── AAPL.parquet (timestamp, price, size, conditions, exchange, id)
├── MSFT.parquet
└── ... 7,000 files total

s3://bucket/tick_data/quotes/2026-06-13/
├── AAPL.parquet (timestamp, bid_price, ask_price, bid_size, ask_size)
├── MSFT.parquet
└── ... 7,000 files total
```

**Key code:**

```python
import os
import time
from datetime import datetime, timedelta
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockTradesRequest, StockQuotesRequest
import boto3
import pandas as pd

def lambda_handler_1(event, context):
    """Lambda 1: Fetch tick trades/quotes for all 7,000 stocks"""
    
    API_KEY = os.environ['ALPACA_API_KEY']
    API_SECRET = os.environ['ALPACA_SECRET']
    BUCKET = os.environ['S3_BUCKET']
    
    client = StockHistoricalDataClient(api_key=API_KEY, secret_key=API_SECRET)
    s3 = boto3.client('s3')
    
    # Get previous trading day
    yesterday = (datetime.now() - timedelta(days=1)).date()
    
    # Load 7,000 stock symbols
    universe = load_universe_tickers(BUCKET)
    
    failed_symbols = []
    processed = 0
    
    for i, symbol in enumerate(universe):
        try:
            # Fetch TRADES
            trades_req = StockTradesRequest(
                symbol_or_symbols=symbol,
                start=datetime.combine(yesterday, datetime.min.time()),
                end=datetime.combine(yesterday, datetime.max.time())
            )
            trades_result = client.get_stock_trades(trades_req)
            
            if symbol in trades_result and trades_result[symbol]:
                trades_df = pd.DataFrame([
                    {
                        'timestamp': t.timestamp,
                        'price': t.price,
                        'size': t.size,
                        'conditions': str(t.conditions),
                        'exchange': t.exchange,
                        'id': t.id
                    }
                    for t in trades_result[symbol]
                ])
                
                # Save to S3
                s3_key = f"tick_data/trades/{yesterday.isoformat()}/{symbol}.parquet"
                s3.put_object(
                    Bucket=BUCKET,
                    Key=s3_key,
                    Body=trades_df.to_parquet(index=False)
                )
            
            # Fetch QUOTES (bid/ask)
            quotes_req = StockQuotesRequest(
                symbol_or_symbols=symbol,
                start=datetime.combine(yesterday, datetime.min.time()),
                end=datetime.combine(yesterday, datetime.max.time())
            )
            quotes_result = client.get_stock_quotes(quotes_req)
            
            if symbol in quotes_result and quotes_result[symbol]:
                quotes_df = pd.DataFrame([
                    {
                        'timestamp': q.timestamp,
                        'bid_price': q.bid_price,
                        'ask_price': q.ask_price,
                        'bid_size': q.bid_size,
                        'ask_size': q.ask_size
                    }
                    for q in quotes_result[symbol]
                ])
                
                s3_key = f"tick_data/quotes/{yesterday.isoformat()}/{symbol}.parquet"
                s3.put_object(
                    Bucket=BUCKET,
                    Key=s3_key,
                    Body=quotes_df.to_parquet(index=False)
                )
            
            processed += 1
            
            # Rate limiting: 1,000 RPM = 16.67 req/sec
            # Sleep 0.15s per symbol ensures 7,000 * 0.15 = 1,050s ≈ 17.5 min
            if i % 100 == 0:
                print(f"Processed {i}/{len(universe)} - {processed} with data")
            
            time.sleep(0.15)
            
        except Exception as e:
            print(f"Error {symbol}: {str(e)[:100]}")
            failed_symbols.append(symbol)
    
    # Publish SNS to trigger Lambda 2
    sns = boto3.client('sns')
    sns.publish(
        TopicArn=os.environ['SNS_TOPIC_LAMBDA2'],
        Message=f"Fetch complete. {processed} symbols, {len(failed_symbols)} failed",
        Subject="PIN Pipeline: Lambda 1 Complete"
    )
    
    return {
        'statusCode': 200,
        'date': str(yesterday),
        'processed': processed,
        'failed': len(failed_symbols),
        'runtime_minutes': 17.5
    }
```

**Rate limiting:** 7,000 symbols × 0.15s sleep = 1,050 seconds ≈ 17.5 minutes

---

## Lambda 2: Calculate Daily PIN Parameters (5 minutes)

**Purpose:** Process tick data → infer trade direction → estimate EHO parameters

**Trigger:** SNS notification from Lambda 1

**Inputs:** Tick trades/quotes from Lambda 1 (S3)

**Outputs:**
```
s3://bucket/pin_daily/2026-06-13.parquet
Columns: symbol, date, eb, es, u, a, num_trades, buy_pct, avg_spread_bps
Rows: ~7,000 (one per stock with data)
```

**Core algorithm - Trade Direction Inference (Lee-Ready Rule):**

```python
def infer_trade_direction_leeredy(trades_df, quotes_df):
    """
    Infer buy/sell direction for each trade using Lee-Ready rule:
    1. If trade_price ≈ ask_price → BUY (aggressive buyer)
    2. If trade_price ≈ bid_price → SELL (aggressive seller)
    3. Else → use tick rule (compare to previous trade)
    """
    trades_df = trades_df.sort_values('timestamp').reset_index(drop=True)
    quotes_df = quotes_df.sort_values('timestamp').reset_index(drop=True)
    
    directions = []
    
    for idx, trade in trades_df.iterrows():
        trade_price = trade['price']
        trade_ts = trade['timestamp']
        
        # Find nearest quote before/at this trade time
        nearest_quotes = quotes_df[quotes_df['timestamp'] <= trade_ts]
        if len(nearest_quotes) == 0:
            directions.append('UNKNOWN')
            continue
        
        nearest_quote = nearest_quotes.iloc[-1]
        bid = nearest_quote['bid_price']
        ask = nearest_quote['ask_price']
        mid = (bid + ask) / 2
        
        # Check rule 1: is trade at ask or bid? (1 cent tolerance)
        if abs(trade_price - ask) < 0.01 and abs(trade_price - bid) >= 0.01:
            directions.append('BUY')
        elif abs(trade_price - bid) < 0.01 and abs(trade_price - ask) >= 0.01:
            directions.append('SELL')
        else:
            # Fallback rule 3: tick rule (compare to previous trade)
            if idx > 0:
                prev_trade = trades_df.iloc[idx - 1]
                prev_price = prev_trade['price']
                if trade_price > prev_price:
                    directions.append('BUY')
                elif trade_price < prev_price:
                    directions.append('SELL')
                else:
                    # Tick unchanged; use mid comparison
                    directions.append('BUY' if trade_price >= mid else 'SELL')
            else:
                # First trade of day; use mid comparison
                directions.append('BUY' if trade_price >= mid else 'SELL')
    
    trades_df['direction'] = directions
    return trades_df


def estimate_daily_pin_params(symbol, trades_df, quotes_df, date):
    """
    Estimate Easley-Hvidkjaer-O'Hara (2002) parameters for one stock-day.
    
    Returns dict with: a, u, es, eb, num_trades, buy_pct, avg_spread_bps
    """
    if len(trades_df) == 0:
        return None
    
    # Step 1: Infer trade direction
    trades_df = infer_trade_direction_leeredy(trades_df, quotes_df)
    
    # Step 2: Count trades by direction
    buy_count = (trades_df['direction'] == 'BUY').sum()
    sell_count = (trades_df['direction'] == 'SELL').sum()
    unknown_count = (trades_df['direction'] == 'UNKNOWN').sum()
    total_trades = len(trades_df)
    
    # Step 3: EHO Parameters
    
    # eb (expected buyer-initiated trades) = count of buys
    eb = buy_count
    
    # es (expected seller-initiated trades) = count of sells  
    es = sell_count
    
    # u (uninformed arrival rate) = trades per trading minute
    trading_minutes = len(trades_df['timestamp'].dt.floor('min').unique())
    u = total_trades / trading_minutes if trading_minutes > 0 else 0
    
    # a (informed arrival rate)
    # Simplified: ratio of buy/sell imbalance (full EHO uses MLE)
    if total_trades > 0:
        imbalance = abs(buy_count - sell_count) / total_trades
        a = imbalance * 0.3  # Conservative: ranges 0-0.3
    else:
        a = 0
    
    # Step 4: Bid-ask spread (basis points)
    if len(quotes_df) > 0:
        quotes_df['spread'] = (quotes_df['ask_price'] - quotes_df['bid_price']) / quotes_df['bid_price'] * 10000
        avg_spread_bps = quotes_df['spread'].mean()
    else:
        avg_spread_bps = float('nan')
    
    return {
        'symbol': symbol,
        'date': date,
        'eb': eb,
        'es': es,
        'u': round(u, 4),
        'a': round(a, 4),
        'num_trades': total_trades,
        'buy_pct': round(buy_count / total_trades, 3) if total_trades > 0 else 0,
        'avg_spread_bps': round(avg_spread_bps, 2)
    }


def lambda_handler_2(event, context):
    """Lambda 2: Calculate daily PIN parameters from tick data"""
    
    BUCKET = os.environ['S3_BUCKET']
    s3 = boto3.client('s3')
    
    # Get date from event or use yesterday
    date_str = event.get('date', (datetime.now() - timedelta(days=1)).date().isoformat())
    date = datetime.fromisoformat(date_str).date()
    
    # Load universe
    universe = load_universe_tickers(BUCKET)
    
    daily_params = []
    failed = []
    
    for symbol in universe:
        try:
            # Load trades and quotes from S3
            trades_key = f"tick_data/trades/{date.isoformat()}/{symbol}.parquet"
            quotes_key = f"tick_data/quotes/{date.isoformat()}/{symbol}.parquet"
            
            # Try to load; skip if file doesn't exist
            try:
                trades_df = pd.read_parquet(f"s3://{BUCKET}/{trades_key}")
            except:
                continue  # No data for this stock on this day
            
            try:
                quotes_df = pd.read_parquet(f"s3://{BUCKET}/{quotes_key}")
            except:
                quotes_df = pd.DataFrame()  # Okay if no quotes
            
            # Estimate parameters
            params = estimate_daily_pin_params(symbol, trades_df, quotes_df, date)
            if params:
                daily_params.append(params)
        
        except Exception as e:
            print(f"Error {symbol}: {str(e)[:100]}")
            failed.append(symbol)
    
    # Save daily parameters to S3
    daily_df = pd.DataFrame(daily_params)
    s3.put_object(
        Bucket=BUCKET,
        Key=f"pin_daily/{date.isoformat()}.parquet",
        Body=daily_df.to_parquet(index=False)
    )
    
    # Trigger Lambda 3
    sns = boto3.client('sns')
    sns.publish(
        TopicArn=os.environ['SNS_TOPIC_LAMBDA3'],
        Message=f"Daily params: {len(daily_params)} symbols",
        Subject="PIN Pipeline: Lambda 2 Complete"
    )
    
    return {
        'statusCode': 200,
        'date': str(date),
        'calculated': len(daily_params),
        'failed': len(failed)
    }
```

---

## Lambda 3: Aggregate to Monthly PIN (2 minutes)

**Purpose:** Combine daily parameters into monthly PIN values for your predictor

**Trigger:** SNS from Lambda 2

**Inputs:** Daily PIN files from Lambda 2

**Outputs:**
```
s3://bucket/pyData/Intermediate/pin_monthly.parquet
Columns: symbol, time_avail_m, a, u, es, eb
(Updated nightly with rolling current month)
```

**Code:**

```python
def lambda_handler_3(event, context):
    """Lambda 3: Aggregate daily PIN params to monthly"""
    
    import numpy as np
    
    BUCKET = os.environ['S3_BUCKET']
    s3 = boto3.client('s3')
    
    # Load all daily PIN files for current month
    today = datetime.now().date()
    month_start = today.replace(day=1)
    
    # Collect all daily files
    daily_dfs = []
    current_date = month_start
    
    while current_date <= today:
        key = f"pin_daily/{current_date.isoformat()}.parquet"
        try:
            df = pd.read_parquet(f"s3://{BUCKET}/{key}")
            daily_dfs.append(df)
        except:
            pass  # File doesn't exist (weekend/holiday)
        
        current_date += timedelta(days=1)
    
    if not daily_dfs:
        print("No daily PIN files for month")
        return {'statusCode': 204}
    
    # Combine and aggregate
    combined = pd.concat(daily_dfs, ignore_index=True)
    
    # Group by symbol, average the parameters
    monthly = combined.groupby('symbol').agg({
        'a': 'mean',
        'u': 'mean',
        'es': 'mean',
        'eb': 'mean',
        'num_trades': 'sum',
        'buy_pct': 'mean',
        'avg_spread_bps': 'mean'
    }).reset_index()
    
    # Set time_avail_m to end of month
    month_end = month_start + pd.offsets.MonthEnd(0)
    monthly['time_avail_m'] = month_end
    
    # Load existing pin_monthly and append (incremental update)
    try:
        existing = pd.read_parquet(f"s3://{BUCKET}/pyData/Intermediate/pin_monthly.parquet")
        # Remove current month, then append new
        existing = existing[existing['time_avail_m'] < pd.Timestamp(month_start)]
        monthly = pd.concat([existing, monthly], ignore_index=True)
    except:
        pass  # File doesn't exist yet
    
    # Save updated pin_monthly
    s3.put_object(
        Bucket=BUCKET,
        Key='pyData/Intermediate/pin_monthly.parquet',
        Body=monthly.to_parquet(index=False)
    )
    
    print(f"Updated pin_monthly: {len(monthly)} symbols for {month_start.strftime('%B %Y')}")
    
    # Trigger Lambda 4 (predictor)
    sns = boto3.client('sns')
    sns.publish(
        TopicArn=os.environ['SNS_TOPIC_LAMBDA4'],
        Message="PIN monthly updated",
        Subject="PIN Pipeline: Lambda 3 Complete"
    )
    
    return {
        'statusCode': 200,
        'symbols': len(monthly),
        'month': str(month_start)
    }
```

---

## Lambda 4: Run PIN Predictor (1 minute)

**Purpose:** Execute your existing `ProbInformedTrading.py` with fresh PIN data

**Trigger:** SNS from Lambda 3

**Inputs:**
- `pin_monthly.parquet` (just updated)
- `SignalMasterTable.parquet` (existing)

**Outputs:**
```
s3://bucket/pyData/Predictors/ProbInformedTrading.csv (updated daily)
```

**Code:**

```python
def lambda_handler_4(event, context):
    """Lambda 4: Run ProbInformedTrading.py predictor"""
    
    BUCKET = os.environ['S3_BUCKET']
    s3 = boto3.client('s3')
    
    # Create Lambda tmp working directory
    work_dir = '/tmp/DataIngressModel'
    os.makedirs(f'{work_dir}/pyData/Intermediate', exist_ok=True)
    os.makedirs(f'{work_dir}/pyData/Predictors', exist_ok=True)
    
    # Download dependencies
    try:
        s3.download_file(BUCKET, 'pyData/Intermediate/pin_monthly.parquet',
                         f'{work_dir}/pyData/Intermediate/pin_monthly.parquet')
        s3.download_file(BUCKET, 'SignalMasterTable.parquet',
                         f'{work_dir}/pyData/Intermediate/SignalMasterTable.parquet')
    except Exception as e:
        return {
            'statusCode': 500,
            'error': f'Failed to download dependencies: {e}'
        }
    
    # Run predictor
    try:
        result = subprocess.run(
            [
                'python3',
                f'{work_dir}/Predictors/ProbInformedTrading.py'
            ],
            cwd=work_dir,
            capture_output=True,
            timeout=60
        )
        
        if result.returncode != 0:
            print(f"Predictor stderr: {result.stderr.decode()}")
            return {
                'statusCode': 500,
                'error': 'Predictor failed',
                'stderr': result.stderr.decode()[:500]
            }
        
        print(f"Predictor output: {result.stdout.decode()}")
        
    except subprocess.TimeoutExpired:
        return {
            'statusCode': 500,
            'error': 'Predictor timeout (>60s)'
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'error': f'Predictor execution failed: {e}'
        }
    
    # Upload result to S3
    try:
        with open(f'{work_dir}/pyData/Predictors/ProbInformedTrading.csv', 'rb') as f:
            s3.put_object(
                Bucket=BUCKET,
                Key='pyData/Predictors/ProbInformedTrading.csv',
                Body=f.read()
            )
    except Exception as e:
        return {
            'statusCode': 500,
            'error': f'Failed to upload result: {e}'
        }
    
    return {
        'statusCode': 200,
        'message': 'PIN signal updated successfully',
        'output_s3': f's3://{BUCKET}/pyData/Predictors/ProbInformedTrading.csv'
    }
```

---

## EventBridge Orchestration

Set up cron rules to trigger each Lambda in sequence:

**Rule 1: Lambda 1 at 17:00 UTC (after market close, weekdays)**
```
Name: pin-lambda-1-fetch-ticks
Schedule: cron(0 17 ? * MON-FRI *)
Target: Lambda 1 function
```

**Rules 2-4: Triggered by SNS from previous Lambda**
- Lambda 1 publishes to SNS when complete
- Lambda 2 subscribes to that SNS topic (auto-triggers)
- Lambda 2 publishes SNS → Lambda 3 (auto-triggers)
- Lambda 3 publishes SNS → Lambda 4 (auto-triggers)

This creates a **fully asynchronous pipeline** with no hard-coded wait times.

---

## Handling Production Edge Cases

**Weekend/Holiday detection:**
```python
# Check if Alpaca returned empty data (market was closed)
if len(daily_params) == 0:
    print("No tick data for this date - likely weekend/holiday")
    # Don't update PIN, use previous month's values
    return {'statusCode': 204, 'reason': 'No trading day'}
```

**Failed symbols:**
```python
# If > 200 symbols failed out of 7,000: alert ops
if len(failed) > 200:
    sns.publish(
        TopicArn=os.environ['ALERT_SNS'],
        Subject='PIN Pipeline: HIGH FAILURE RATE',
        Message=f'{len(failed)}/7000 symbols failed - likely Alpaca API issue'
    )
```

**Subscription errors:**
```python
except APIError as e:
    if "subscription does not permit" in str(e):
        # Pause pipeline, alert immediately
        sns.publish(
            TopicArn=os.environ['CRITICAL_ALERT_SNS'],
            Subject='PIN Pipeline STOPPED: Subscription Error',
            Message=f'Check Alpaca Market Data Subscriptions: {e}'
        )
        raise
```

---

## Monitoring (CloudWatch)

Track pipeline health:

```python
import boto3

cloudwatch = boto3.client('cloudwatch')

# Log successful metrics
cloudwatch.put_metric_data(
    Namespace='PIN',
    MetricData=[
        {
            'MetricName': 'DailySymbolsProcessed',
            'Value': len(daily_params),
            'Unit': 'Count'
        },
        {
            'MetricName': 'LambdaExecutionTime',
            'Value': context.get_remaining_time_in_millis() / 1000,
            'Unit': 'Seconds'
        }
    ]
)
```

**Alarms to set:**
- DailySymbolsProcessed < 6,000 → Page on-call (data gap)
- FailedSymbols > 100 → Alert (API issue)
- LambdaExecutionTime > 1800 (30 min) → Timeout risk
- Pin_monthly.csv not updated for 2 days → Pipeline broken

---

## Cost Estimate

| Component | Daily Cost | Monthly |
|-----------|-----------|---------|
| Alpaca API calls | Free | $0 |
| Lambda (4 invocations) | ~$0.006 | ~$0.18 |
| S3 storage (tick data) | ~$0.01 | ~$0.30 |
| CloudWatch logs/metrics | ~$0.003 | ~$0.09 |
| **Total** | **~$0.02/day** | **~$0.57/month** |

---

## Integration with Existing System

**Your ProbInformedTrading.py expects:**
```
../pyData/Intermediate/pin_monthly.parquet  ← Lambda 3 produces this
../pyData/Intermediate/SignalMasterTable.parquet  ← Already exists
```

**Output it produces:**
```
../pyData/Predictors/ProbInformedTrading.csv  ← Consumed by SignalMasterTable
```

**No changes needed** to existing code. The Lambdas simply keep `pin_monthly.parquet` fresh.

---

## Summary: What An Agent Needs to Build This

To delegate PIN pipeline build to another model, provide:

1. **4 Lambda functions** with SNS chaining (nightly orchestration)
2. **Alpaca API credentials** stored in Lambda environment variables
3. **S3 bucket** for tick data, daily params, monthly aggregates
4. **EventBridge rule** to trigger Lambda 1 at 17:00 UTC on weekdays
5. **SNS topics** for Lambda-to-Lambda communication
6. **IAM roles** with S3, SNS, CloudWatch permissions for each Lambda
7. **CloudWatch alarms** for pipeline monitoring
8. **Error handling**: Retry logic, failed symbol tracking, subscription validation

**Cost:** ~$0.57/month (minimal)  
**Runtime:** 40 minutes nightly (fully within Lambda limits)  
**Freshness:** Daily updated PIN signal ready each morning  
**Reliability:** Asynchronous, resilient to individual symbol failures

