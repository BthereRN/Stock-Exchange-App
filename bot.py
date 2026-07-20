
import discord
from discord.ext import commands, tasks
import psycopg2
import os
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ── Web server (keeps bot alive on free hosting) ──────────────────────────────
app = Flask(__name__)

@app.route('/')
def home():
    return "Obelisk Stock Exchange Bot is running!"

def run_server():
    app.run(host='0.0.0.0', port=5000)

Thread(target=run_server, daemon=True).start()

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

ADMIN_IDS = [909889735038746694]
OWNERSHIP_CAP = 0.40

# ── Database connection ───────────────────────────────────────────────────────
DATABASE_URL = os.environ['DATABASE_URL']

def make_connection():
    global DATABASE_URL
    
    if not DATABASE_URL:
        DATABASE_URL = os.environ.get('DATABASE_URL')
        
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing or empty!")

    # Clean up any wrapping quotes or accidental spaces
    url_str = DATABASE_URL.strip().strip('"').strip("'")
    
    # Strip out the protocol prefix safely
    if url_str.startswith("postgres://"):
        url_str = url_str[11:]
    elif url_str.startswith("postgresql://"):
        url_str = url_str[13:]

    try:
        # 1. Split at the absolute LAST '@' to separate the password from the host address
        credentials, host_part = url_str.rsplit('@', 1)
        
        # 2. Separate username and password at the first ':'
        username, password = credentials.split(':', 1)
        
        # 3. Separate host/port from the database name at the first '/'
        host_and_port, database = host_part.split('/', 1)
        
        # 4. Separate hostname and port number if a port exists
        if ':' in host_and_port:
            hostname, port = host_and_port.rsplit(':', 1)
            port = int(port)
        else:
            hostname = host_and_port
            port = 5432
            
        # Hand-deliver the individual components explicitly 
        c = psycopg2.connect(
            host=hostname,
            database=database,
            user=username,
            password=password,
            port=port,
            connect_timeout=10
        )
        
        c.autocommit = False  # Preserving your exact engine workflow
        return c
        
    except Exception as e:
        print(f"[ERROR] Failed custom parsing connection logic: {e}")
        raise e

conn = make_connection()
cursor = conn.cursor()

def ensure_connection():
    global conn, cursor
    try:
        cursor.execute("SELECT 1")
    except (psycopg2.OperationalError, psycopg2.InterfaceError, psycopg2.DatabaseError):
        conn = make_connection()
        cursor = conn.cursor()

# ── Table creation ────────────────────────────────────────────────────────────
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    discord_id TEXT PRIMARY KEY,
    cash DOUBLE PRECISION DEFAULT 0.0
)""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS stocks (
    ticker TEXT PRIMARY KEY,
    company_name TEXT,
    current_price DOUBLE PRECISION,
    total_shares INTEGER DEFAULT 0,
    company_networth DOUBLE PRECISION DEFAULT 0.0,
    auto_price INTEGER DEFAULT 1,
    last_price_update TEXT,
    previous_price DOUBLE PRECISION DEFAULT 0.0,
    ipo_price DOUBLE PRECISION DEFAULT NULL,
    ipo_shares INTEGER DEFAULT 0
)""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS portfolios (
    discord_id TEXT,
    ticker TEXT,
    shares INTEGER,
    PRIMARY KEY (discord_id, ticker)
)""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    discord_id TEXT,
    ticker TEXT,
    type TEXT,
    shares INTEGER,
    price DOUBLE PRECISION,
    timestamp TEXT
)""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    discord_id TEXT,
    ticker TEXT,
    type TEXT,
    shares INTEGER,
    filled INTEGER DEFAULT 0,
    price DOUBLE PRECISION,
    status TEXT DEFAULT 'open',
    placed_at TEXT
)""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
)""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS alerts (
    id SERIAL PRIMARY KEY,
    discord_id TEXT,
    ticker TEXT,
    target_price DOUBLE PRECISION,
    direction TEXT,
    created_at TEXT
)""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS fee_income (
    id SERIAL PRIMARY KEY,
    ticker TEXT,
    amount DOUBLE PRECISION,
    timestamp TEXT
)""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS watchlists (
    discord_id TEXT,
    ticker TEXT,
    PRIMARY KEY (discord_id, ticker)
)""")

conn.commit()

# ── Migrations (safe on existing DB) ─────────────────────────────────────────
for col, col_type in [
    ("company_networth",  "DOUBLE PRECISION DEFAULT 0.0"),
    ("auto_price",        "INTEGER DEFAULT 1"),
    ("last_price_update", "TEXT"),
    ("previous_price",    "DOUBLE PRECISION DEFAULT 0.0"),
    ("ipo_price",         "DOUBLE PRECISION DEFAULT NULL"),
    ("ipo_shares",        "INTEGER DEFAULT 0"),
    ("ipo_owner_id",      "TEXT DEFAULT NULL"),
]:
    try:
        cursor.execute(f"ALTER TABLE stocks ADD COLUMN IF NOT EXISTS {col} {col_type}")
        conn.commit()
    except Exception:
        conn.rollback()

# ── Seed config defaults ──────────────────────────────────────────────────────
for key, default in [
    ("fee_percent",       "2.0"),
    ("price_channel_id",  ""),
    ("price_interval_days", "3"),
    ("order_expiry_days", "7"),
]:
    cursor.execute("INSERT INTO config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (key, default))
conn.commit()

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_registered(discord_id):
    ensure_connection()
    cursor.execute("SELECT cash FROM users WHERE discord_id = %s", (str(discord_id),))
    return cursor.fetchone() is not None

def get_config(key):
    ensure_connection()
    cursor.execute("SELECT value FROM config WHERE key = %s", (key,))
    row = cursor.fetchone()
    return row[0] if row else None

def get_fee():
    try:
        return float(get_config("fee_percent")) / 100.0
    except (TypeError, ValueError):
        return 0.02

def credit_fee_to_admin(amount):
    admin_id = str(ADMIN_IDS[0])
    cursor.execute("SELECT cash FROM users WHERE discord_id = %s", (admin_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("UPDATE users SET cash = cash + %s WHERE discord_id = %s", (amount, admin_id))
    else:
        cursor.execute("INSERT INTO users (discord_id, cash) VALUES (%s, %s)", (admin_id, amount))

def get_shares_owned(discord_id, ticker):
    cursor.execute("SELECT shares FROM portfolios WHERE discord_id = %s AND ticker = %s", (str(discord_id), ticker))
    row = cursor.fetchone()
    return row[0] if row else 0

def get_shares_in_sell_orders(discord_id, ticker):
    cursor.execute(
        "SELECT COALESCE(SUM(shares - filled), 0) FROM orders "
        "WHERE discord_id = %s AND ticker = %s AND type = 'sell' AND status IN ('open', 'partial')",
        (str(discord_id), ticker)
    )
    return cursor.fetchone()[0] or 0

def get_total_shares_held(discord_id, ticker):
    return get_shares_owned(discord_id, ticker) + get_shares_in_sell_orders(discord_id, ticker)

def get_shares_in_buy_orders(discord_id, ticker):
    cursor.execute(
        "SELECT COALESCE(SUM(shares - filled), 0) FROM orders "
        "WHERE discord_id = %s AND ticker = %s AND type = 'buy' AND status IN ('open', 'partial')",
        (str(discord_id), ticker)
    )
    return cursor.fetchone()[0] or 0

async def fetch_display_name(guild, discord_id: str) -> str:
    member = guild.get_member(int(discord_id))
    if member:
        return member.display_name
    try:
        fetched = await guild.fetch_member(int(discord_id))
        return fetched.display_name
    except Exception:
        pass
    try:
        user = await bot.fetch_user(int(discord_id))
        return user.name
    except Exception:
        return "Unknown User"

# ── Price alert checker (called after every trade) ────────────────────────────

async def check_price_alerts(ticker: str, new_price: float, guild):
    cursor.execute(
        "SELECT id, discord_id, target_price, direction FROM alerts WHERE ticker = %s",
        (ticker,)
    )
    triggered = []
    for alert_id, discord_id, target_price, direction in cursor.fetchall():
        hit = (direction == 'above' and new_price >= target_price) or \
              (direction == 'below' and new_price <= target_price)
        if hit:
            triggered.append((alert_id, discord_id, target_price, direction))

    for alert_id, discord_id, target_price, direction in triggered:
        cursor.execute("DELETE FROM alerts WHERE id = %s", (alert_id,))
        member = guild.get_member(int(discord_id))
        if not member:
            try:
                member = await bot.fetch_user(int(discord_id))
            except Exception:
                continue
        try:
            arrow = "📈" if direction == 'above' else "📉"
            await member.send(
                f"{arrow} **Price Alert — {ticker}**\n"
                f"The price has {'risen to' if direction == 'above' else 'fallen to'} "
                f"**${new_price:,.2f}** (your target: **${target_price:,.2f}**)."
            )
        except discord.Forbidden:
            pass

    if triggered:
        conn.commit()

# ── Order matching engine ─────────────────────────────────────────────────────

async def match_orders(ticker, guild):
    ensure_connection()

    cursor.execute("SELECT total_shares FROM stocks WHERE ticker = %s", (ticker,))
    stock_row = cursor.fetchone()
    if not stock_row:
        return
    total_shares = stock_row[0]

    last_exec_price = None

    while True:
        cursor.execute("""
            SELECT id, discord_id, shares - filled, price
            FROM orders
            WHERE ticker = %s AND type = 'sell' AND status IN ('open', 'partial')
            ORDER BY price ASC, placed_at ASC
            LIMIT 1
        """, (ticker,))
        best_sell = cursor.fetchone()

        cursor.execute("""
            SELECT id, discord_id, shares - filled, price
            FROM orders
            WHERE ticker = %s AND type = 'buy' AND status IN ('open', 'partial')
            ORDER BY price DESC, placed_at ASC
            LIMIT 1
        """, (ticker,))
        best_buy = cursor.fetchone()

        if not best_sell or not best_buy:
            break

        sell_id, seller_id, sell_remaining, sell_price = best_sell
        buy_id,  buyer_id,  buy_remaining,  buy_price  = best_buy

        if buy_price < sell_price:
            break
        if buyer_id == seller_id:
            break

        exec_price = sell_price

        buyer_total_held = get_total_shares_held(buyer_id, ticker)
        max_allowed = int(total_shares * OWNERSHIP_CAP)
        cap_remaining = max_allowed - buyer_total_held

        if cap_remaining <= 0:
            cursor.execute("SELECT shares - filled, price FROM orders WHERE id = %s", (buy_id,))
            leftover_qty, leftover_price = cursor.fetchone()
            buyer_fee_rate = 0.0 if buyer_id == str(ADMIN_IDS[0]) else get_fee()
            refund = round(leftover_qty * leftover_price * (1 + buyer_fee_rate), 2)
            cursor.execute("UPDATE users SET cash = cash + %s WHERE discord_id = %s", (refund, buyer_id))
            cursor.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (buy_id,))
            conn.commit()
            member = guild.get_member(int(buyer_id))
            if member:
                try:
                    await member.send(
                        f"⚠️ Your buy order for **{ticker}** was cancelled — you've hit the "
                        f"**{int(OWNERSHIP_CAP * 100)}% ownership cap**. "
                        f"**${refund:,.2f}** returned to your account."
                    )
                except discord.Forbidden:
                    pass
            continue

        trade_qty = min(sell_remaining, buy_remaining, cap_remaining)
        fee_rate = get_fee()
        admin_str = str(ADMIN_IDS[0])
        buyer_fee  = 0.0 if buyer_id  == admin_str else fee_rate
        seller_fee = 0.0 if seller_id == admin_str else fee_rate

        buyer_refund = round((buy_price - exec_price) * trade_qty * (1 + buyer_fee), 2)
        if buyer_refund > 0:
            cursor.execute("UPDATE users SET cash = cash + %s WHERE discord_id = %s", (buyer_refund, buyer_id))

        seller_receives = round(exec_price * trade_qty * (1 - seller_fee), 2)
        cursor.execute("UPDATE users SET cash = cash + %s WHERE discord_id = %s", (seller_receives, seller_id))

        total_fee = round(exec_price * trade_qty * (buyer_fee + seller_fee), 2)
        if total_fee > 0:
            credit_fee_to_admin(total_fee)
            cursor.execute(
                "INSERT INTO fee_income (ticker, amount, timestamp) VALUES (%s, %s, %s)",
                (ticker, total_fee, datetime.utcnow().isoformat())
            )

        buyer_current = get_shares_owned(buyer_id, ticker)
        if buyer_current > 0:
            cursor.execute("UPDATE portfolios SET shares = shares + %s WHERE discord_id = %s AND ticker = %s",
                           (trade_qty, buyer_id, ticker))
        else:
            cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (%s, %s, %s)",
                           (buyer_id, ticker, trade_qty))

        now_iso = datetime.utcnow().isoformat()
        cursor.execute(
            "INSERT INTO transactions (discord_id, ticker, type, shares, price, timestamp) VALUES (%s,%s,%s,%s,%s,%s)",
            (buyer_id,  ticker, "buy",  trade_qty, exec_price, now_iso)
        )
        cursor.execute(
            "INSERT INTO transactions (discord_id, ticker, type, shares, price, timestamp) VALUES (%s,%s,%s,%s,%s,%s)",
            (seller_id, ticker, "sell", trade_qty, exec_price, now_iso)
        )

        cursor.execute("SELECT filled, shares FROM orders WHERE id = %s", (sell_id,))
        s_filled, s_total = cursor.fetchone()
        new_s_filled = s_filled + trade_qty
        cursor.execute("UPDATE orders SET filled = %s, status = %s WHERE id = %s",
                       (new_s_filled, 'filled' if new_s_filled >= s_total else 'partial', sell_id))

        cursor.execute("SELECT filled, shares FROM orders WHERE id = %s", (buy_id,))
        b_filled, b_total = cursor.fetchone()
        new_b_filled = b_filled + trade_qty
        cursor.execute("UPDATE orders SET filled = %s, status = %s WHERE id = %s",
                       (new_b_filled, 'filled' if new_b_filled >= b_total else 'partial', buy_id))

        cursor.execute(
            "UPDATE stocks SET previous_price = current_price, current_price = %s, last_price_update = %s WHERE ticker = %s",
            (exec_price, now_iso, ticker)
        )
        conn.commit()

        last_exec_price = exec_price

        fee_str = get_config("fee_percent")
        buyer_cost  = round(exec_price * trade_qty * (1 + buyer_fee),  2)
        buyer_note  = f"(no fee — exchange admin)" if buyer_fee  == 0.0 else f"(incl. {fee_str}% fee)"
        seller_note = f"(no fee — exchange admin)" if seller_fee == 0.0 else f"(after {fee_str}% fee)"
        for did, msg in [
            (buyer_id,
             f"✅ **Order Filled!** Bought **{trade_qty:,}** shares of **{ticker}** at **${exec_price:,.2f}**.\n"
             f"Total cost: **${buyer_cost:,.2f}** {buyer_note}."),
            (seller_id,
             f"💸 **Order Filled!** Sold **{trade_qty:,}** shares of **{ticker}** at **${exec_price:,.2f}**.\n"
             f"You received: **${seller_receives:,.2f}** {seller_note}.")
        ]:
            member = guild.get_member(int(did))
            if member:
                try:
                    await member.send(msg)
                except discord.Forbidden:
                    pass

    if last_exec_price is not None:
        await check_price_alerts(ticker, last_exec_price, guild)

# ── Background tasks ──────────────────────────────────────────────────────────

@tasks.loop(hours=1)
async def maintenance_task():
    """Hourly: expire stale orders + auto-price illiquid stocks."""
    ensure_connection()
    now = datetime.utcnow()

    # ── Order expiry ──────────────────────────────────────────────────────────
    try:
        expiry_days = int(get_config("order_expiry_days") or 7)
    except (ValueError, TypeError):
        expiry_days = 7

    if expiry_days > 0:
        cutoff = (now - timedelta(days=expiry_days)).isoformat()
        fee_rate = get_fee()

        cursor.execute(
            "SELECT id, discord_id, ticker, type, shares, filled, price FROM orders "
            "WHERE status IN ('open','partial') AND placed_at < %s",
            (cutoff,)
        )
        stale = cursor.fetchall()

        for oid, did, ticker, otype, tot, filled, price in stale:
            remaining = tot - filled
            cursor.execute("UPDATE orders SET status = 'expired' WHERE id = %s", (oid,))
            if otype == 'buy':
                order_fee_rate = 0.0 if did == str(ADMIN_IDS[0]) else fee_rate
                refund = round(remaining * price * (1 + order_fee_rate), 2)
                cursor.execute("UPDATE users SET cash = cash + %s WHERE discord_id = %s", (refund, did))
                try:
                    user = await bot.fetch_user(int(did))
                    await user.send(
                        f"⏰ Your **buy order** for **{remaining:,}** shares of **{ticker}** at **${price:,.2f}** "
                        f"expired after {expiry_days} days. **${refund:,.2f}** returned to your account."
                    )
                except Exception:
                    pass
            else:
                cursor.execute("SELECT shares FROM portfolios WHERE discord_id = %s AND ticker = %s", (did, ticker))
                p = cursor.fetchone()
                if p:
                    cursor.execute("UPDATE portfolios SET shares = shares + %s WHERE discord_id = %s AND ticker = %s",
                                   (remaining, did, ticker))
                else:
                    cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (%s, %s, %s)",
                                   (did, ticker, remaining))
                try:
                    user = await bot.fetch_user(int(did))
                    await user.send(
                        f"⏰ Your **sell order** for **{remaining:,}** shares of **{ticker}** at **${price:,.2f}** "
                        f"expired after {expiry_days} days. Shares returned to your portfolio."
                    )
                except Exception:
                    pass

        if stale:
            conn.commit()
            print(f"[Expiry] Cancelled {len(stale)} stale order(s).")

    # ── Auto-price (reference price for illiquid stocks) ─────────────────────
    try:
        interval_days = int(get_config("price_interval_days") or 3)
    except (ValueError, TypeError):
        interval_days = 3
    interval_ago = (now - timedelta(days=interval_days)).isoformat()

    cursor.execute(
        "SELECT ticker, company_name, current_price, total_shares, last_price_update FROM stocks WHERE auto_price = 1"
    )
    stocks = cursor.fetchall()

    price_channel_id = get_config("price_channel_id")
    channel = None
    if price_channel_id:
        try:
            channel = bot.get_channel(int(price_channel_id))
        except (ValueError, TypeError):
            pass

    for ticker, name, price, total, last_update in stocks:
        if last_update:
            try:
                if datetime.fromisoformat(last_update) > now - timedelta(days=interval_days):
                    continue
            except ValueError:
                pass

        cursor.execute(
            "SELECT COUNT(*) FROM transactions WHERE ticker = %s AND timestamp >= %s",
            (ticker, interval_ago)
        )
        if cursor.fetchone()[0] > 0:
            cursor.execute("UPDATE stocks SET last_price_update = %s WHERE ticker = %s", (now.isoformat(), ticker))
            conn.commit()
            continue

        cursor.execute(
            "SELECT COALESCE(SUM(shares - filled), 0) FROM orders WHERE ticker = %s AND type = 'buy' AND status IN ('open','partial')",
            (ticker,)
        )
        buy_demand = cursor.fetchone()[0] or 0

        cursor.execute(
            "SELECT COALESCE(SUM(shares - filled), 0) FROM orders WHERE ticker = %s AND type = 'sell' AND status IN ('open','partial')",
            (ticker,)
        )
        sell_supply = cursor.fetchone()[0] or 0

        if buy_demand == 0 and sell_supply == 0:
            cursor.execute("UPDATE stocks SET last_price_update = %s WHERE ticker = %s", (now.isoformat(), ticker))
            conn.commit()
            continue

        net = buy_demand - sell_supply
        demand_ratio = max(-0.25, min(0.25, (net / total) * 0.5 if total > 0 else 0))
        new_price = round(max(0.01, price * (1 + demand_ratio)), 2)

        cursor.execute(
            "UPDATE stocks SET current_price = %s, previous_price = %s, last_price_update = %s WHERE ticker = %s",
            (new_price, price, now.isoformat(), ticker)
        )
        conn.commit()
        print(f"[Auto-Price] {ticker}: ${price:.2f} → ${new_price:.2f}")

        if channel and new_price != price and price > 0:
            pct = ((new_price - price) / price) * 100
            direction = "📈" if new_price > price else "📉"
            await channel.send(
                f"{direction} **Reference Price Update — {name} ({ticker})**\n"
                f"No recent trades. Reference adjusted **${price:,.2f}** → **${new_price:,.2f}** ({pct:+.1f}%)."
            )

# ── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} - Obelisk Stock Exchange Active!")
    maintenance_task.start()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`. Use `!help` to see correct usage.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument type. Use `!help` to see correct usage.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    elif isinstance(error, commands.CommandInvokeError):
        print(f"[Error] Command '{ctx.command}' raised: {error.original}")
        await ctx.send("❌ Something went wrong running that command. Please try again.")
    else:
        print(f"[Error] Unhandled error in '{ctx.command}': {error}")

# ── User Commands ─────────────────────────────────────────────────────────────

@bot.command(name="help")
async def help_command(ctx):
    fee = get_config("fee_percent")
    try:
        expiry = int(get_config("order_expiry_days") or 7)
    except (ValueError, TypeError):
        expiry = 7
    embed = discord.Embed(
        title="📖 Obelisk Stock Exchange — Command Guide",
        description=(
            "Buyers are matched with sellers via a real order-book. "
            f"A **{fee}%** fee applies to both sides of every completed trade. "
            f"Open orders expire after **{expiry} days** if unfilled."
        ),
        color=discord.Color.green()
    )
    embed.add_field(name="!market", value="All stocks with last price, best bid/ask, IPO status.", inline=False)
    embed.add_field(name="!balance", value="Your cash, holdings, locked orders, and net worth.", inline=False)
    embed.add_field(name="!buy [TICKER] [shares] [price]", value="Place a limit buy order. Cash reserved immediately. `!buy ABC 10 15.50`", inline=False)
    embed.add_field(name="!sell [TICKER] [shares] [price]", value="Place a limit sell order. Shares reserved immediately. `!sell ABC 5 20.00`", inline=False)
    embed.add_field(name="!buyipo [TICKER] [shares]", value="Buy during an active IPO at the fixed price. `!buyipo ABC 10`", inline=False)
    embed.add_field(name="!myorders", value="View all your open orders.", inline=False)
    embed.add_field(name="!cancelorder [ID]", value="Cancel one open order and reclaim cash/shares. `!cancelorder 42`", inline=False)
    embed.add_field(name="!cancelall", value="Cancel all your open orders at once.", inline=False)
    embed.add_field(name="!history", value="Your last 15 completed trades.", inline=False)
    embed.add_field(name="!trades [TICKER]", value="Last 10 completed trades for a stock. `!trades ABC`", inline=False)
    embed.add_field(name="!chart [TICKER]", value="Price chart image for a stock (last 50 trades). `!chart ABC`", inline=False)
    embed.add_field(name="!companyinfo [TICKER]", value="Company details, order book depth, largest holder. `!companyinfo ABC`", inline=False)
    embed.add_field(name="!shareholders [TICKER]", value="Full ranked shareholder list with percentages. `!shareholders ABC`", inline=False)
    embed.add_field(name="!alert [TICKER] [price]", value="DM alert when a stock hits a target price. `!alert ABC 25.00`", inline=False)
    embed.add_field(name="!myalerts", value="View your active price alerts.", inline=False)
    embed.add_field(name="!cancelalert [ID]", value="Remove a price alert. `!cancelalert 3`", inline=False)
    embed.add_field(name="!leaderboard", value="Top 10 wealthiest traders by net worth.", inline=False)
    embed.add_field(name="!watch [TICKER]", value="Add a stock to your personal watchlist. `!watch ABC`", inline=False)
    embed.add_field(name="!unwatch [TICKER]", value="Remove a stock from your watchlist. `!unwatch ABC`", inline=False)
    embed.add_field(name="!watchlist", value="Your watchlist — prices, bid/ask, your position, and % change.", inline=False)
    embed.set_footer(text=f"No account? Open a support ticket. Max {int(OWNERSHIP_CAP*100)}% ownership per company.")
    await ctx.send(embed=embed)


@bot.command()
async def market(ctx):
    ensure_connection()
    cursor.execute("SELECT ticker, company_name, current_price, previous_price, total_shares, auto_price, ipo_price, ipo_shares FROM stocks")
    all_stocks = cursor.fetchall()

    embed = discord.Embed(title="🏛️ Obelisk Stock Exchange", color=discord.Color.gold())

    if all_stocks:
        for ticker, name, price, prev_price, total, auto, ipo_price, ipo_shares in all_stocks:
            cursor.execute(
                "SELECT MAX(price) FROM orders WHERE ticker = %s AND type = 'buy' AND status IN ('open','partial')", (ticker,)
            )
            best_bid = cursor.fetchone()[0]
            cursor.execute(
                "SELECT MIN(price) FROM orders WHERE ticker = %s AND type = 'sell' AND status IN ('open','partial')", (ticker,)
            )
            best_ask = cursor.fetchone()[0]

            auto_tag = " 🤖" if auto else ""
            change_tag = ""
            if prev_price and prev_price > 0 and prev_price != price:
                pct = ((price - prev_price) / prev_price) * 100
                change_tag = f" ({'📈' if pct > 0 else '📉'} {pct:+.1f}%)"

            bid_str = f"${best_bid:,.2f}" if best_bid else "—"
            ask_str = f"${best_ask:,.2f}" if best_ask else "—"

            value = (
                f"Last: **${price:,.2f}**{change_tag}{auto_tag} | Bid: **{bid_str}** | Ask: **{ask_str}**\n"
                f"Total shares: **{total:,}**"
            )
            if ipo_price and ipo_shares and ipo_shares > 0:
                value += f"\n🔓 **IPO Active** — {ipo_shares:,} shares @ **${ipo_price:,.2f}** (use `!buyipo {ticker}`)"

            embed.add_field(name=f"{name} ({ticker})", value=value, inline=False)
    else:
        embed.description = "No stocks are currently listed."

    fee = get_config("fee_percent")
    embed.set_footer(text=f"{fee}% fee on all trades (both sides). Place orders with !buy / !sell.")
    await ctx.send(embed=embed)


@bot.command()
async def balance(ctx):
    ensure_connection()
    if not is_registered(ctx.author.id):
        await ctx.send("❌ You don't have an exchange account. Open a support ticket to get started!")
        return

    uid = str(ctx.author.id)
    cursor.execute("SELECT cash FROM users WHERE discord_id = %s", (uid,))
    cash = cursor.fetchone()[0]

    cursor.execute("""
        SELECT p.ticker, p.shares, s.current_price
        FROM portfolios p JOIN stocks s ON p.ticker = s.ticker
        WHERE p.discord_id = %s AND p.shares > 0
    """, (uid,))
    holdings = cursor.fetchall()

    cursor.execute("""
        SELECT id, ticker, type, shares, filled, price
        FROM orders WHERE discord_id = %s AND status IN ('open','partial')
        ORDER BY placed_at ASC
    """, (uid,))
    open_orders = cursor.fetchall()

    embed = discord.Embed(title=f"💼 {ctx.author.name}'s Account", color=discord.Color.blue())
    embed.add_field(name="Available Cash", value=f"${cash:,.2f}", inline=True)

    is_admin_user = ctx.author.id in ADMIN_IDS
    fee_rate = get_fee()
    effective_fee = 0.0 if is_admin_user else fee_rate
    locked_cash = sum((r[2] - r[3]) * r[4] * (1 + effective_fee) for r in open_orders if r[2] == 'buy')
    if locked_cash > 0:
        embed.add_field(name="Locked in Buy Orders", value=f"${locked_cash:,.2f}", inline=True)

    total_value = cash + locked_cash

    if holdings:
        hstr = ""
        for ticker, shares, price in holdings:
            val = shares * price
            total_value += val
            hstr += f"**{ticker}**: {shares:,} shares ≈ ${val:,.2f}\n"
        embed.add_field(name="Stocks Owned", value=hstr, inline=False)
    else:
        embed.add_field(name="Stocks Owned", value="None", inline=False)

    if open_orders:
        ostr = ""
        for oid, ticker, otype, shares, filled, price in open_orders:
            remaining = shares - filled
            ostr += f"**#{oid}** {otype.upper()} {remaining:,} {ticker} @ ${price:,.2f}\n"
        embed.add_field(name="Open Orders", value=ostr, inline=False)

    embed.add_field(name="Estimated Net Worth", value=f"${total_value:,.2f}", inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def buy(ctx, ticker: str, shares: int, price: float):
    """Place a limit buy order. Usage: !buy ABC 10 15.50"""
    ensure_connection()
    if not is_registered(ctx.author.id):
        await ctx.send("❌ You don't have an account yet.")
        return
    if shares <= 0:
        await ctx.send("❌ Shares must be greater than 0.")
        return
    if price <= 0:
        await ctx.send("❌ Price must be greater than 0.")
        return

    ticker = ticker.upper()
    uid = str(ctx.author.id)

    cursor.execute("SELECT company_name, total_shares FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** is not listed.")
        return
    company_name, total_shares = row

    total_held = get_total_shares_held(uid, ticker)
    pending_buys = get_shares_in_buy_orders(uid, ticker)
    max_allowed = int(total_shares * OWNERSHIP_CAP)
    if total_held + pending_buys + shares > max_allowed:
        allowed_now = max_allowed - total_held - pending_buys
        if allowed_now <= 0:
            await ctx.send(f"❌ You've reached the **{int(OWNERSHIP_CAP*100)}% ownership cap** for **{ticker}**.")
            return
        await ctx.send(
            f"❌ This order would exceed the **{int(OWNERSHIP_CAP*100)}% cap** for **{ticker}**. "
            f"You can order at most **{allowed_now:,}** more shares."
        )
        return

    is_admin_buyer = ctx.author.id in ADMIN_IDS
    fee_rate = 0.0 if is_admin_buyer else get_fee()
    reserve_amount = round(shares * price * (1 + fee_rate), 2)

    cursor.execute("SELECT cash FROM users WHERE discord_id = %s", (uid,))
    cash = cursor.fetchone()[0]
    if cash < reserve_amount:
        fee_note = "no fee — you are the exchange admin" if is_admin_buyer else f"incl. {get_config('fee_percent')}% fee"
        await ctx.send(
            f"❌ Insufficient funds. Need **${reserve_amount:,.2f}** ({fee_note}). "
            f"You have **${cash:,.2f}**."
        )
        return

    cursor.execute("UPDATE users SET cash = cash - %s WHERE discord_id = %s", (reserve_amount, uid))
    cursor.execute(
        "INSERT INTO orders (discord_id, ticker, type, shares, filled, price, status, placed_at) VALUES (%s,%s,'buy',%s,0,%s,'open',%s)",
        (uid, ticker, shares, price, datetime.utcnow().isoformat())
    )
    conn.commit()

    fee_note = "no fee — exchange admin" if is_admin_buyer else f"incl. {get_config('fee_percent')}% fee"
    await ctx.send(
        f"📋 Buy order placed: **{shares:,}** shares of **{company_name} ({ticker})** @ **${price:,.2f}**. "
        f"**${reserve_amount:,.2f}** reserved ({fee_note}). Waiting for a matching seller..."
    )
    await match_orders(ticker, ctx.guild)


@bot.command()
async def sell(ctx, ticker: str, shares: int, price: float):
    """Place a limit sell order. Usage: !sell ABC 5 20.00"""
    ensure_connection()
    if not is_registered(ctx.author.id):
        await ctx.send("❌ You don't have an account yet.")
        return
    if shares <= 0:
        await ctx.send("❌ Shares must be greater than 0.")
        return
    if price <= 0:
        await ctx.send("❌ Price must be greater than 0.")
        return

    ticker = ticker.upper()
    uid = str(ctx.author.id)

    cursor.execute("SELECT company_name FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** is not listed.")
        return
    company_name = row[0]

    free_shares = get_shares_owned(uid, ticker)
    if free_shares < shares:
        await ctx.send(
            f"❌ You only have **{free_shares:,}** freely available shares of **{ticker}** "
            f"(shares already in sell orders are excluded)."
        )
        return

    new_shares = free_shares - shares
    if new_shares == 0:
        cursor.execute("DELETE FROM portfolios WHERE discord_id = %s AND ticker = %s", (uid, ticker))
    else:
        cursor.execute("UPDATE portfolios SET shares = %s WHERE discord_id = %s AND ticker = %s", (new_shares, uid, ticker))

    cursor.execute(
        "INSERT INTO orders (discord_id, ticker, type, shares, filled, price, status, placed_at) VALUES (%s,%s,'sell',%s,0,%s,'open',%s)",
        (uid, ticker, shares, price, datetime.utcnow().isoformat())
    )
    conn.commit()

    await ctx.send(
        f"📋 Sell order placed: **{shares:,}** shares of **{company_name} ({ticker})** @ **${price:,.2f}**. "
        f"Shares reserved. Waiting for a matching buyer..."
    )
    await match_orders(ticker, ctx.guild)


@bot.command()
async def buyipo(ctx, ticker: str, shares: int):
    """Buy shares during an active IPO at the fixed offering price. Usage: !buyipo ABC 10"""
    ensure_connection()
    if not is_registered(ctx.author.id):
        await ctx.send("❌ You don't have an account yet.")
        return
    if shares <= 0:
        await ctx.send("❌ Shares must be greater than 0.")
        return

    ticker = ticker.upper()
    uid = str(ctx.author.id)

    cursor.execute("SELECT company_name, total_shares, ipo_price, ipo_shares, ipo_owner_id FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** is not listed.")
        return

    company_name, total_shares, ipo_price, ipo_shares, ipo_owner_id = row

    if not ipo_price or not ipo_shares or ipo_shares <= 0:
        await ctx.send(f"❌ **{ticker}** does not have an active IPO. Use `!buy {ticker} {shares} [price]` to place a market order.")
        return

    if shares > ipo_shares:
        await ctx.send(
            f"❌ Only **{ipo_shares:,}** IPO shares remain for **{ticker}**. "
            f"Adjust your quantity or buy the rest on the open market."
        )
        return

    total_held = get_total_shares_held(uid, ticker)
    pending_buys = get_shares_in_buy_orders(uid, ticker)
    max_allowed = int(total_shares * OWNERSHIP_CAP)
    if total_held + pending_buys + shares > max_allowed:
        allowed_now = max_allowed - total_held - pending_buys
        if allowed_now <= 0:
            await ctx.send(f"❌ You've reached the **{int(OWNERSHIP_CAP*100)}% ownership cap** for **{ticker}**.")
            return
        await ctx.send(
            f"❌ IPO purchase would exceed the **{int(OWNERSHIP_CAP*100)}% cap**. "
            f"You can buy at most **{allowed_now:,}** more shares."
        )
        return

    fee_rate = get_fee()
    total_cost = round(shares * ipo_price * (1 + fee_rate), 2)

    cursor.execute("SELECT cash FROM users WHERE discord_id = %s", (uid,))
    cash = cursor.fetchone()[0]
    if cash < total_cost:
        await ctx.send(
            f"❌ Insufficient funds. **{shares:,}** shares × **${ipo_price:,.2f}** + fee = **${total_cost:,.2f}**. "
            f"You have **${cash:,.2f}**."
        )
        return

    cursor.execute("UPDATE users SET cash = cash - %s WHERE discord_id = %s", (total_cost, uid))

    existing = get_shares_owned(uid, ticker)
    if existing > 0:
        cursor.execute("UPDATE portfolios SET shares = shares + %s WHERE discord_id = %s AND ticker = %s",
                       (shares, uid, ticker))
    else:
        cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (%s, %s, %s)", (uid, ticker, shares))

    fee_amount = round(shares * ipo_price * fee_rate, 2)
    credit_fee_to_admin(fee_amount)
    if fee_amount > 0:
        cursor.execute(
            "INSERT INTO fee_income (ticker, amount, timestamp) VALUES (%s, %s, %s)",
            (ticker, fee_amount, datetime.utcnow().isoformat())
        )

    new_ipo_shares = ipo_shares - shares
    cursor.execute("UPDATE stocks SET ipo_shares = %s WHERE ticker = %s", (new_ipo_shares, ticker))
    if new_ipo_shares == 0:
        cursor.execute("UPDATE stocks SET ipo_price = NULL WHERE ticker = %s", (ticker,))

    cursor.execute(
        "INSERT INTO transactions (discord_id, ticker, type, shares, price, timestamp) VALUES (%s,%s,'buy',%s,%s,%s)",
        (uid, ticker, shares, ipo_price, datetime.utcnow().isoformat())
    )
    conn.commit()

    msg = (
        f"🎉 **IPO Purchase!** You bought **{shares:,}** shares of **{company_name} ({ticker})** "
        f"at the IPO price of **${ipo_price:,.2f}** each. Total: **${total_cost:,.2f}** (incl. fee)."
    )
    if new_ipo_shares == 0:
        msg += f"\n📢 The **{ticker}** IPO is now **sold out**! Normal trading has begun."
    else:
        msg += f"\n*{new_ipo_shares:,} IPO shares still available.*"

    await ctx.send(msg)


@bot.command()
async def myorders(ctx):
    ensure_connection()
    uid = str(ctx.author.id)
    cursor.execute("""
        SELECT id, ticker, type, shares, filled, price, placed_at
        FROM orders WHERE discord_id = %s AND status IN ('open','partial')
        ORDER BY placed_at ASC
    """, (uid,))
    rows = cursor.fetchall()

    if not rows:
        await ctx.send("You have no open orders.")
        return

    fee_rate = get_fee()
    embed = discord.Embed(title=f"📋 {ctx.author.name}'s Open Orders", color=discord.Color.blue())
    for oid, ticker, otype, tot, filled, price, placed_at in rows:
        remaining = tot - filled
        date_str = placed_at[:10] if placed_at else "?"
        if otype == 'buy':
            locked = round(remaining * price * (1 + fee_rate), 2)
            detail = f"**{remaining:,}** shares @ **${price:,.2f}** | Cash locked: **${locked:,.2f}**"
        else:
            detail = f"**{remaining:,}** shares @ **${price:,.2f}** | Shares reserved"
        if filled > 0:
            detail += f" | Partially filled: {filled:,}"
        embed.add_field(name=f"#{oid} — {otype.upper()} {ticker} (placed {date_str})", value=detail, inline=False)

    embed.set_footer(text="Use !cancelorder [ID] or !cancelall to cancel.")
    await ctx.send(embed=embed)


@bot.command()
async def cancelorder(ctx, order_id: int):
    """Cancel an open order. Usage: !cancelorder 42"""
    ensure_connection()
    uid = str(ctx.author.id)
    cursor.execute(
        "SELECT ticker, type, shares, filled, price, status FROM orders WHERE id = %s AND discord_id = %s",
        (order_id, uid)
    )
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ Order **#{order_id}** not found or doesn't belong to you.")
        return

    ticker, otype, tot, filled, price, status = row
    if status not in ('open', 'partial'):
        await ctx.send(f"❌ Order **#{order_id}** is already **{status}**.")
        return

    remaining = tot - filled
    cancel_fee = 0.0 if ctx.author.id in ADMIN_IDS else get_fee()
    cursor.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (order_id,))

    if otype == 'buy':
        refund = round(remaining * price * (1 + cancel_fee), 2)
        cursor.execute("UPDATE users SET cash = cash + %s WHERE discord_id = %s", (refund, uid))
        conn.commit()
        await ctx.send(f"✅ Buy order **#{order_id}** cancelled. **${refund:,.2f}** returned to your account.")
    else:
        p = get_shares_owned(uid, ticker)
        if p > 0:
            cursor.execute("UPDATE portfolios SET shares = shares + %s WHERE discord_id = %s AND ticker = %s",
                           (remaining, uid, ticker))
        else:
            cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (%s, %s, %s)",
                           (uid, ticker, remaining))
        conn.commit()
        await ctx.send(f"✅ Sell order **#{order_id}** cancelled. **{remaining:,}** shares of **{ticker}** returned to your portfolio.")


@bot.command()
async def cancelall(ctx):
    """Cancel all your open orders at once."""
    ensure_connection()
    uid = str(ctx.author.id)
    cursor.execute(
        "SELECT id, ticker, type, shares, filled, price FROM orders WHERE discord_id = %s AND status IN ('open','partial')",
        (uid,)
    )
    open_orders = cursor.fetchall()

    if not open_orders:
        await ctx.send("You have no open orders to cancel.")
        return

    cancelall_fee = 0.0 if ctx.author.id in ADMIN_IDS else get_fee()
    total_cash_refund = 0.0
    share_returns = {}

    for oid, ticker, otype, tot, filled, price in open_orders:
        remaining = tot - filled
        cursor.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (oid,))
        if otype == 'buy':
            total_cash_refund += remaining * price * (1 + cancelall_fee)
        else:
            share_returns[ticker] = share_returns.get(ticker, 0) + remaining

    if total_cash_refund > 0:
        total_cash_refund = round(total_cash_refund, 2)
        cursor.execute("UPDATE users SET cash = cash + %s WHERE discord_id = %s", (total_cash_refund, uid))

    for ticker, qty in share_returns.items():
        p = get_shares_owned(uid, ticker)
        if p > 0:
            cursor.execute("UPDATE portfolios SET shares = shares + %s WHERE discord_id = %s AND ticker = %s",
                           (qty, uid, ticker))
        else:
            cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (%s, %s, %s)",
                           (uid, ticker, qty))

    conn.commit()

    parts = []
    if total_cash_refund > 0:
        parts.append(f"**${total_cash_refund:,.2f}** cash returned")
    for ticker, qty in share_returns.items():
        parts.append(f"**{qty:,}** {ticker} shares returned")

    await ctx.send(
        f"✅ Cancelled **{len(open_orders)}** open order(s). " + " | ".join(parts) + "."
    )


@bot.command()
async def history(ctx):
    """View your last 15 completed trades."""
    ensure_connection()
    uid = str(ctx.author.id)
    cursor.execute("""
        SELECT ticker, type, shares, price, timestamp
        FROM transactions WHERE discord_id = %s
        ORDER BY timestamp DESC LIMIT 15
    """, (uid,))
    rows = cursor.fetchall()

    if not rows:
        await ctx.send("You have no completed trades yet.")
        return

    embed = discord.Embed(title=f"📜 {ctx.author.name}'s Trade History", color=discord.Color.purple())
    lines = []
    for ticker, ttype, shares, price, timestamp in rows:
        try:
            dt = datetime.fromisoformat(timestamp)
            time_str = dt.strftime("%b %d, %H:%M")
        except (ValueError, TypeError):
            time_str = str(timestamp)[:16]
        icon = "🟢" if ttype == "buy" else "🔴"
        lines.append(f"{icon} `{time_str}` — {ttype.upper()} **{int(shares):,}** {ticker} @ **${price:,.2f}**")

    embed.description = "\n".join(lines)
    embed.set_footer(text="Most recent first. Shows last 15 trades.")
    await ctx.send(embed=embed)


@bot.command()
async def trades(ctx, ticker: str):
    """Show the last 10 completed trades for a stock. Usage: !trades ABC"""
    ensure_connection()
    ticker = ticker.upper()

    cursor.execute("SELECT company_name FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** is not listed.")
        return

    company_name = row[0]
    cursor.execute("""
        SELECT shares, price, timestamp FROM transactions
        WHERE ticker = %s AND type = 'buy'
        ORDER BY timestamp DESC LIMIT 10
    """, (ticker,))
    rows = cursor.fetchall()

    if not rows:
        await ctx.send(f"No completed trades for **{company_name} ({ticker})** yet.")
        return

    embed = discord.Embed(title=f"📈 Recent Trades — {company_name} ({ticker})", color=discord.Color.teal())
    lines = []
    for shares, price, timestamp in rows:
        try:
            dt = datetime.fromisoformat(timestamp)
            time_str = dt.strftime("%b %d, %H:%M UTC")
        except (ValueError, TypeError):
            time_str = str(timestamp)[:16]
        lines.append(f"`{time_str}` — **{int(shares):,} shares** @ **${price:,.2f}**")

    embed.description = "\n".join(lines)
    embed.set_footer(text="Most recent trades shown first.")
    await ctx.send(embed=embed)


@bot.command()
async def alert(ctx, ticker: str, target_price: float):
    """Set a price alert for a stock. Usage: !alert ABC 25.00"""
    ensure_connection()
    ticker = ticker.upper()

    cursor.execute("SELECT current_price FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** is not listed.")
        return

    current_price = row[0]
    direction = 'above' if target_price > current_price else 'below'

    cursor.execute(
        "INSERT INTO alerts (discord_id, ticker, target_price, direction, created_at) VALUES (%s,%s,%s,%s,%s)",
        (str(ctx.author.id), ticker, target_price, direction, datetime.utcnow().isoformat())
    )
    conn.commit()

    arrow = "📈" if direction == 'above' else "📉"
    await ctx.send(
        f"{arrow} Alert set! You'll receive a DM when **{ticker}** "
        f"{'rises to' if direction == 'above' else 'falls to'} **${target_price:,.2f}** "
        f"(currently **${current_price:,.2f}**)."
    )


@bot.command()
async def myalerts(ctx):
    """View your active price alerts."""
    ensure_connection()
    cursor.execute(
        "SELECT id, ticker, target_price, direction FROM alerts WHERE discord_id = %s ORDER BY created_at ASC",
        (str(ctx.author.id),)
    )
    rows = cursor.fetchall()

    if not rows:
        await ctx.send("You have no active price alerts. Use `!alert [TICKER] [price]` to set one.")
        return

    embed = discord.Embed(title=f"🔔 {ctx.author.name}'s Price Alerts", color=discord.Color.orange())
    for alert_id, ticker, target_price, direction in rows:
        cursor.execute("SELECT current_price FROM stocks WHERE ticker = %s", (ticker,))
        current_row = cursor.fetchone()
        current = f"currently **${current_row[0]:,.2f}**" if current_row else "stock not found"
        arrow = "📈" if direction == 'above' else "📉"
        embed.add_field(
            name=f"#{alert_id} — {ticker} {arrow}",
            value=f"Alert when {'above' if direction == 'above' else 'below'} **${target_price:,.2f}** ({current})",
            inline=False
        )

    embed.set_footer(text="Use !cancelalert [ID] to remove an alert.")
    await ctx.send(embed=embed)


@bot.command()
async def cancelalert(ctx, alert_id: int):
    """Remove a price alert. Usage: !cancelalert 3"""
    ensure_connection()
    cursor.execute(
        "SELECT ticker, target_price FROM alerts WHERE id = %s AND discord_id = %s",
        (alert_id, str(ctx.author.id))
    )
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ Alert **#{alert_id}** not found or doesn't belong to you.")
        return

    cursor.execute("DELETE FROM alerts WHERE id = %s", (alert_id,))
    conn.commit()
    await ctx.send(f"✅ Alert **#{alert_id}** for **{row[0]}** @ **${row[1]:,.2f}** removed.")


@bot.command()
async def companyinfo(ctx, ticker: str):
    ensure_connection()
    ticker = ticker.upper()
    cursor.execute("""
        SELECT company_name, current_price, previous_price, total_shares, company_networth,
               auto_price, last_price_update, ipo_price, ipo_shares
        FROM stocks WHERE ticker = %s
    """, (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** is not listed.")
        return

    name, price, prev_price, total, networth, auto, last_update, ipo_price, ipo_shares = row
    market_cap = total * price

    embed = discord.Embed(title=f"🏢 {name} ({ticker})", color=discord.Color.teal())
    embed.add_field(name="Last Trade Price", value=f"${price:,.2f}", inline=True)
    embed.add_field(name="Market Cap", value=f"${market_cap:,.2f}", inline=True)
    if networth and networth > 0:
        embed.add_field(name="Company Net Worth", value=f"${networth:,.2f}", inline=True)
    embed.add_field(name="Total Shares", value=f"{total:,}", inline=True)
    embed.add_field(name="Ownership Cap", value=f"{int(OWNERSHIP_CAP*100)}% = {int(total * OWNERSHIP_CAP):,} shares", inline=True)

    if prev_price and prev_price > 0 and prev_price != price:
        pct = ((price - prev_price) / prev_price) * 100
        embed.add_field(name="Price Change", value=f"{'📈' if pct > 0 else '📉'} {pct:+.1f}%", inline=True)

    if ipo_price and ipo_shares and ipo_shares > 0:
        embed.add_field(
            name="🔓 Active IPO",
            value=f"**{ipo_shares:,}** shares available at **${ipo_price:,.2f}** — use `!buyipo {ticker} [shares]`",
            inline=False
        )

    cursor.execute("""
        SELECT price, SUM(shares - filled) FROM orders
        WHERE ticker = %s AND type = 'buy' AND status IN ('open','partial')
        GROUP BY price ORDER BY price DESC LIMIT 3
    """, (ticker,))
    bids = cursor.fetchall()

    cursor.execute("""
        SELECT price, SUM(shares - filled) FROM orders
        WHERE ticker = %s AND type = 'sell' AND status IN ('open','partial')
        GROUP BY price ORDER BY price ASC LIMIT 3
    """, (ticker,))
    asks = cursor.fetchall()

    bid_str = "\n".join(f"${p:,.2f} × {int(q):,}" for p, q in bids) if bids else "No buy orders"
    ask_str = "\n".join(f"${p:,.2f} × {int(q):,}" for p, q in asks) if asks else "No sell orders"
    embed.add_field(name="📗 Top Bids", value=bid_str, inline=True)
    embed.add_field(name="📕 Top Asks", value=ask_str, inline=True)

    cursor.execute("""
        SELECT discord_id, SUM(shares) FROM (
            SELECT discord_id, shares FROM portfolios WHERE ticker = %s
            UNION ALL
            SELECT discord_id, shares - filled FROM orders WHERE ticker = %s AND type = 'sell' AND status IN ('open','partial')
        ) combined GROUP BY discord_id ORDER BY SUM(shares) DESC LIMIT 1
    """, (ticker, ticker))
    top = cursor.fetchone()
    if top:
        holder_name = await fetch_display_name(ctx.guild, top[0])
        pct = (top[1] / total * 100) if total > 0 else 0
        embed.add_field(name="Largest Shareholder", value=f"{holder_name} — {int(top[1]):,} shares ({pct:.1f}%)", inline=False)

    try:
        interval_days = int(get_config("price_interval_days") or 3)
    except (ValueError, TypeError):
        interval_days = 3
    pricing_mode = f"🤖 Auto reference ({interval_days}-day cycle)" if auto else "🔧 Manual reference price only"
    embed.add_field(name="Pricing Mode", value=pricing_mode, inline=False)
    embed.set_footer(text="Prices are set by real trades. Use !buy and !sell to participate.")
    await ctx.send(embed=embed)


@bot.command()
async def leaderboard(ctx):
    ensure_connection()
    cursor.execute("SELECT discord_id, cash FROM users")
    all_users = cursor.fetchall()
    if not all_users:
        await ctx.send("No accounts exist yet.")
        return

    fee_rate = get_fee()
    net_worths = []
    for discord_id, cash in all_users:
        cursor.execute("""
            SELECT p.shares, s.current_price FROM portfolios p JOIN stocks s ON p.ticker = s.ticker
            WHERE p.discord_id = %s AND p.shares > 0
        """, (discord_id,))
        stock_value = sum(sh * pr for sh, pr in cursor.fetchall())

        cursor.execute("""
            SELECT o.shares - o.filled, s.current_price FROM orders o JOIN stocks s ON o.ticker = s.ticker
            WHERE o.discord_id = %s AND o.type = 'sell' AND o.status IN ('open','partial')
        """, (discord_id,))
        sell_value = sum(sh * pr for sh, pr in cursor.fetchall())

        cursor.execute(
            "SELECT COALESCE(SUM((shares - filled) * price * %s), 0) FROM orders "
            "WHERE discord_id = %s AND type = 'buy' AND status IN ('open','partial')",
            (1 + fee_rate, discord_id)
        )
        locked_cash = cursor.fetchone()[0] or 0

        net_worths.append((discord_id, cash + stock_value + sell_value + locked_cash))

    net_worths.sort(key=lambda x: x[1], reverse=True)
    embed = discord.Embed(title="🏆 Obelisk Wealth Leaderboard", color=discord.Color.gold())
    medals = ["🥇", "🥈", "🥉"]
    for i, (discord_id, nw) in enumerate(net_worths[:10]):
        name = await fetch_display_name(ctx.guild, discord_id)
        prefix = medals[i] if i < 3 else f"**#{i+1}**"
        embed.add_field(name=f"{prefix} {name}", value=f"${nw:,.2f}", inline=False)

    await ctx.send(embed=embed)

# ── Admin Commands ────────────────────────────────────────────────────────────

@bot.command()
async def deposit(ctx, member: discord.Member, amount: float):
    """[Admin] Add funds to a user's account. Usage: !deposit @User 5000"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Only the exchange owner can process deposits.")
        return
    if amount <= 0:
        await ctx.send("❌ Amount must be positive.")
        return

    cursor.execute("SELECT cash FROM users WHERE discord_id = %s", (str(member.id),))
    row = cursor.fetchone()
    if row:
        new_bal = row[0] + amount
        cursor.execute("UPDATE users SET cash = %s WHERE discord_id = %s", (new_bal, str(member.id)))
        await ctx.send(f"💰 Deposited **${amount:,.2f}** to {member.mention}. New balance: **${new_bal:,.2f}**.")
    else:
        cursor.execute("INSERT INTO users (discord_id, cash) VALUES (%s, %s)", (str(member.id), amount))
        await ctx.send(f"🏛️ Account created for {member.mention} with **${amount:,.2f}**.")
    conn.commit()


@bot.command()
async def withdraw(ctx, member: discord.Member, amount: float):
    """[Admin] Deduct funds from a user's account. Usage: !withdraw @User 1000"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Only the exchange owner can process withdrawals.")
        return
    if amount <= 0:
        await ctx.send("❌ Amount must be positive.")
        return

    cursor.execute("SELECT cash FROM users WHERE discord_id = %s", (str(member.id),))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ {member.mention} has no account.")
        return
    if row[0] < amount:
        await ctx.send(f"❌ Insufficient funds. {member.mention} only has **${row[0]:,.2f}**.")
        return

    new_bal = row[0] - amount
    cursor.execute("UPDATE users SET cash = %s WHERE discord_id = %s", (new_bal, str(member.id)))
    conn.commit()
    await ctx.send(f"🏦 Withdrew **${amount:,.2f}** from {member.mention}. New balance: **${new_bal:,.2f}**.")


@bot.command()
async def portfolio(ctx, member: discord.Member):
    """[Admin] View any user's account. Usage: !portfolio @User"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    cursor.execute("SELECT cash FROM users WHERE discord_id = %s", (str(member.id),))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ {member.mention} has no account.")
        return

    cash = row[0]
    cursor.execute("""
        SELECT p.ticker, p.shares, s.current_price FROM portfolios p JOIN stocks s ON p.ticker = s.ticker
        WHERE p.discord_id = %s AND p.shares > 0
    """, (str(member.id),))
    holdings = cursor.fetchall()

    embed = discord.Embed(title=f"🔍 {member.name}'s Account (Admin)", color=discord.Color.red())
    embed.add_field(name="Available Cash", value=f"${cash:,.2f}", inline=False)
    if holdings:
        total_value = cash
        hstr = ""
        for ticker, shares, price in holdings:
            val = shares * price
            total_value += val
            hstr += f"**{ticker}**: {shares:,} shares ≈ ${val:,.2f}\n"
        embed.add_field(name="Stocks Owned", value=hstr, inline=False)
        embed.add_field(name="Est. Net Worth", value=f"${total_value:,.2f}", inline=False)
    else:
        embed.add_field(name="Stocks Owned", value="None", inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def grant(ctx, member: discord.Member, ticker: str, shares: int):
    """[Admin] Grant shares to a user. Usage: !grant @User ABC 20"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if shares <= 0:
        await ctx.send("❌ Shares must be greater than 0.")
        return

    ticker = ticker.upper()
    cursor.execute("SELECT company_name, current_price, total_shares FROM stocks WHERE ticker = %s", (ticker,))
    stock = cursor.fetchone()
    if not stock:
        await ctx.send(f"❌ **{ticker}** does not exist.")
        return

    company_name, price, total_shares = stock
    if not is_registered(member.id):
        await ctx.send(f"❌ {member.mention} has no account. Use `!deposit` first.")
        return

    total_held = get_total_shares_held(str(member.id), ticker)
    max_allowed = int(total_shares * OWNERSHIP_CAP)
    if total_held + shares > max_allowed:
        await ctx.send(
            f"⚠️ This grant would put {member.mention} over the {int(OWNERSHIP_CAP*100)}% cap "
            f"({total_held:,} currently held, cap = {max_allowed:,}). Not processed."
        )
        return

    p = get_shares_owned(str(member.id), ticker)
    if p > 0:
        cursor.execute("UPDATE portfolios SET shares = shares + %s WHERE discord_id = %s AND ticker = %s",
                       (shares, str(member.id), ticker))
    else:
        cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (%s, %s, %s)",
                       (str(member.id), ticker, shares))

    cursor.execute("UPDATE stocks SET total_shares = total_shares + %s WHERE ticker = %s", (shares, ticker))
    conn.commit()
    await ctx.send(
        f"🎟️ **{shares:,} shares** of **{company_name} ({ticker})** granted to {member.mention}. "
        f"Est. value: **${shares * price:,.2f}**."
    )


@bot.command()
async def addstock(ctx, ticker: str, price: float, total_shares: int, *, name: str):
    """[Admin] List a new company. Usage: !addstock ABC 15.5 100 ABC Industries"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if price <= 0:
        await ctx.send("❌ Price must be greater than 0.")
        return
    if total_shares < 0:
        await ctx.send("❌ Share count cannot be negative.")
        return

    ticker = ticker.upper()
    admin_id = str(ADMIN_IDS[0])
    now = datetime.utcnow().isoformat()

    try:
        cursor.execute(
            "INSERT INTO stocks (ticker, company_name, current_price, previous_price, total_shares, company_networth, auto_price, last_price_update) "
            "VALUES (%s, %s, %s, %s, %s, 0.0, 1, %s)",
            (ticker, name, price, price, total_shares, now)
        )

        if total_shares > 0:
            p = get_shares_owned(admin_id, ticker)
            if p > 0:
                cursor.execute("UPDATE portfolios SET shares = shares + %s WHERE discord_id = %s AND ticker = %s",
                               (total_shares, admin_id, ticker))
            else:
                cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (%s, %s, %s)",
                               (admin_id, ticker, total_shares))

        cursor.execute("SELECT cash FROM users WHERE discord_id = %s", (admin_id,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO users (discord_id, cash) VALUES (%s, 0.0)", (admin_id,))

        conn.commit()
        if total_shares > 0:
            await ctx.send(
                f"🏢 **{name} ({ticker})** listed at **${price:,.2f}**. **{total_shares:,}** shares assigned to admin.\n"
                f"Use `!startipo {ticker} {price} {total_shares}` to open an IPO, `!grant` to distribute to founders, "
                f"or sell them on the open market."
            )
        else:
            await ctx.send(
                f"🏢 **{name} ({ticker})** listed at **${price:,.2f}** with no shares issued yet.\n"
                f"Use `!addshares {ticker} [amount]` to issue shares when the company is ready to go public."
            )
    except psycopg2.IntegrityError:
        conn.rollback()
        await ctx.send(f"❌ Ticker **{ticker}** already exists.")


@bot.command()
async def addshares(ctx, ticker: str, amount: int):
    """[Admin] Issue new shares to admin account. Usage: !addshares ABC 50"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if amount <= 0:
        await ctx.send("❌ Amount must be greater than 0.")
        return

    ticker = ticker.upper()
    admin_id = str(ADMIN_IDS[0])

    cursor.execute("SELECT company_name, total_shares FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** does not exist.")
        return

    name, total = row
    cursor.execute("UPDATE stocks SET total_shares = total_shares + %s WHERE ticker = %s", (amount, ticker))

    p = get_shares_owned(admin_id, ticker)
    if p > 0:
        cursor.execute("UPDATE portfolios SET shares = shares + %s WHERE discord_id = %s AND ticker = %s",
                       (amount, admin_id, ticker))
    else:
        cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (%s, %s, %s)",
                       (admin_id, ticker, amount))

    conn.commit()
    await ctx.send(
        f"📈 **{amount:,}** new shares of **{name} ({ticker})** issued. Total supply: **{total + amount:,}**."
    )


@bot.command()
async def removestock(ctx, ticker: str):
    """[Admin] Delist a company. Usage: !removestock ABC"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    ticker = ticker.upper()
    cursor.execute("SELECT company_name, current_price FROM stocks WHERE ticker = %s", (ticker,))
    stock = cursor.fetchone()
    if not stock:
        await ctx.send(f"❌ **{ticker}** does not exist.")
        return

    company_name, price = stock
    fee_rate = get_fee()

    cursor.execute(
        "SELECT id, discord_id, type, shares, filled, price FROM orders WHERE ticker = %s AND status IN ('open','partial')",
        (ticker,)
    )
    for oid, did, otype, tot, filled, oprice in cursor.fetchall():
        remaining = tot - filled
        cursor.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (oid,))
        if otype == 'buy':
            cursor.execute("UPDATE users SET cash = cash + %s WHERE discord_id = %s",
                           (round(remaining * oprice * (1 + fee_rate), 2), did))
        else:
            cursor.execute("UPDATE users SET cash = cash + %s WHERE discord_id = %s",
                           (round(remaining * price, 2), did))

    cursor.execute("SELECT discord_id, shares FROM portfolios WHERE ticker = %s", (ticker,))
    holders = cursor.fetchall()
    for discord_id, shares in holders:
        cursor.execute("UPDATE users SET cash = cash + %s WHERE discord_id = %s",
                       (round(shares * price, 2), discord_id))
        cursor.execute("DELETE FROM portfolios WHERE discord_id = %s AND ticker = %s", (discord_id, ticker))

    cursor.execute("DELETE FROM alerts WHERE ticker = %s", (ticker,))
    cursor.execute("DELETE FROM stocks WHERE ticker = %s", (ticker,))
    conn.commit()

    msg = f"🗑️ **{company_name} ({ticker})** delisted at **${price:,.2f}**. {len(holders)} holder(s) paid out."
    await ctx.send(msg)


@bot.command()
async def setprice(ctx, ticker: str, new_price: float):
    """[Admin] Manually set the reference price. Usage: !setprice ABC 22.0"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    ticker = ticker.upper()
    cursor.execute("SELECT current_price FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send("❌ Ticker not found.")
        return

    cursor.execute(
        "UPDATE stocks SET current_price = %s, previous_price = %s, last_price_update = %s WHERE ticker = %s",
        (new_price, row[0], datetime.utcnow().isoformat(), ticker)
    )
    conn.commit()
    await ctx.send(f"🔧 **{ticker}** reference price set to **${new_price:,.2f}**.")


@bot.command()
async def setnetworth(ctx, ticker: str, networth: float):
    """[Admin] Set a company's net worth. Usage: !setnetworth ABC 50000"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if networth < 0:
        await ctx.send("❌ Net worth cannot be negative.")
        return

    ticker = ticker.upper()
    cursor.execute("SELECT company_name FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** does not exist.")
        return

    cursor.execute("UPDATE stocks SET company_networth = %s WHERE ticker = %s", (networth, ticker))
    conn.commit()
    await ctx.send(f"📋 **{row[0]} ({ticker})** net worth set to **${networth:,.2f}**.")


@bot.command()
async def autoprice(ctx, ticker: str, setting: str):
    """[Admin] Toggle auto reference-price. Usage: !autoprice ABC on/off"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    ticker = ticker.upper()
    setting = setting.lower()
    if setting not in ("on", "off"):
        await ctx.send("❌ Use **on** or **off**.")
        return

    cursor.execute("SELECT company_name FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** does not exist.")
        return

    cursor.execute("UPDATE stocks SET auto_price = %s WHERE ticker = %s", (1 if setting == "on" else 0, ticker))
    conn.commit()
    status = "🤖 enabled" if setting == "on" else "🔧 disabled"
    await ctx.send(f"Auto-pricing for **{row[0]} ({ticker})** is now {status}.")


@bot.command()
async def setfee(ctx, percent: float):
    """[Admin] Set the exchange fee %. Usage: !setfee 3.0"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if percent < 0 or percent > 20:
        await ctx.send("❌ Fee must be between 0% and 20%.")
        return

    cursor.execute("UPDATE config SET value = %s WHERE key = 'fee_percent'", (str(percent),))
    conn.commit()
    await ctx.send(f"💱 Exchange fee updated to **{percent}%** (charged to both sides of each trade).")


@bot.command()
async def setpricechannel(ctx, channel: discord.TextChannel):
    """[Admin] Set channel for auto-price announcements. Usage: !setpricechannel #channel"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    cursor.execute("UPDATE config SET value = %s WHERE key = 'price_channel_id'", (str(channel.id),))
    conn.commit()
    await ctx.send(f"📢 Auto-price announcements will post in {channel.mention}.")


@bot.command()
async def setpriceinterval(ctx, days: int):
    """[Admin] Set the auto-price check interval. Usage: !setpriceinterval 3"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if days < 1 or days > 30:
        await ctx.send("❌ Must be between 1 and 30 days.")
        return

    cursor.execute("UPDATE config SET value = %s WHERE key = 'price_interval_days'", (str(days),))
    conn.commit()
    await ctx.send(f"⏱️ Auto-price interval set to every **{days} day(s)**.")


@bot.command()
async def setexpiry(ctx, days: int):
    """[Admin] Set how many days until unfilled orders expire. 0 = never expire. Usage: !setexpiry 7"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if days < 0 or days > 90:
        await ctx.send("❌ Must be between 0 (disabled) and 90 days.")
        return

    cursor.execute("UPDATE config SET value = %s WHERE key = 'order_expiry_days'", (str(days),))
    conn.commit()
    if days == 0:
        await ctx.send("⏰ Order expiry **disabled**. Open orders will remain until manually cancelled.")
    else:
        await ctx.send(f"⏰ Orders will now expire after **{days} day(s)** if unfilled.")


@bot.command()
async def dividend(ctx, ticker: str, amount_per_share: float):
    """[Admin] Pay a dividend to all current shareholders. Usage: !dividend ABC 2.50"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if amount_per_share <= 0:
        await ctx.send("❌ Dividend amount must be positive.")
        return

    ticker = ticker.upper()
    cursor.execute("SELECT company_name FROM stocks WHERE ticker = %s", (ticker,))
    stock = cursor.fetchone()
    if not stock:
        await ctx.send(f"❌ **{ticker}** does not exist.")
        return

    company_name = stock[0]
    admin_id = str(ADMIN_IDS[0])

    # Collect all holders: portfolio shares + shares locked in sell orders
    cursor.execute("SELECT discord_id, shares FROM portfolios WHERE ticker = %s AND shares > 0", (ticker,))
    portfolio_holders = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute(
        "SELECT discord_id, SUM(shares - filled) FROM orders "
        "WHERE ticker = %s AND type = 'sell' AND status IN ('open','partial') GROUP BY discord_id",
        (ticker,)
    )
    for discord_id, locked_shares in cursor.fetchall():
        portfolio_holders[discord_id] = portfolio_holders.get(discord_id, 0) + int(locked_shares)

    if not portfolio_holders:
        await ctx.send(f"❌ No shareholders found for **{ticker}**.")
        return

    # Don't pay dividend to admin themselves from admin account
    if admin_id in portfolio_holders:
        del portfolio_holders[admin_id]

    if not portfolio_holders:
        await ctx.send("❌ No external shareholders to pay (only admin holds shares).")
        return

    total_payout = sum(shares * amount_per_share for shares in portfolio_holders.values())
    total_payout = round(total_payout, 2)

    # Check admin has enough cash
    cursor.execute("SELECT cash FROM users WHERE discord_id = %s", (admin_id,))
    admin_row = cursor.fetchone()
    admin_cash = admin_row[0] if admin_row else 0.0

    if admin_cash < total_payout:
        await ctx.send(
            f"❌ Insufficient funds in admin account to pay this dividend.\n"
            f"Total payout needed: **${total_payout:,.2f}** | Admin balance: **${admin_cash:,.2f}**."
        )
        return

    cursor.execute("UPDATE users SET cash = cash - %s WHERE discord_id = %s", (total_payout, admin_id))

    now_iso = datetime.utcnow().isoformat()
    recipients = 0
    for discord_id, shares in portfolio_holders.items():
        payment = round(shares * amount_per_share, 2)
        cursor.execute("UPDATE users SET cash = cash + %s WHERE discord_id = %s", (payment, discord_id))
        recipients += 1

    conn.commit()

    summary_lines = [
        f"**{await fetch_display_name(ctx.guild, did)}**: {sh:,} shares × ${amount_per_share:,.2f} = **${round(sh * amount_per_share, 2):,.2f}**"
        for did, sh in sorted(portfolio_holders.items(), key=lambda x: x[1], reverse=True)[:10]
    ]

    embed = discord.Embed(
        title=f"💵 Dividend Paid — {company_name} ({ticker})",
        color=discord.Color.green(),
        description="\n".join(summary_lines) + (f"\n*+ {recipients - 10} more...*" if recipients > 10 else "")
    )
    embed.add_field(name="Per Share", value=f"${amount_per_share:,.2f}", inline=True)
    embed.add_field(name="Total Paid Out", value=f"${total_payout:,.2f}", inline=True)
    embed.add_field(name="Recipients", value=str(recipients), inline=True)
    await ctx.send(embed=embed)


@bot.command()
async def startipo(ctx, owner: discord.Member, ticker: str, ipo_price: float, ipo_shares: int):
    """[Admin] Start an IPO for a company owner at a fixed price. Usage: !startipo @Owner ABC 10.00 500"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if ipo_price <= 0 or ipo_shares <= 0:
        await ctx.send("❌ IPO price and shares must be greater than 0.")
        return

    ticker = ticker.upper()
    owner_id = str(owner.id)

    cursor.execute("SELECT company_name, total_shares FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** does not exist. Create it first with `!addstock`.")
        return

    company_name, total_shares = row

    if not is_registered(owner.id):
        await ctx.send(f"❌ {owner.mention} has no account. They need to `!deposit` first.")
        return

    owner_free_shares = get_shares_owned(owner_id, ticker)
    if owner_free_shares < ipo_shares:
        await ctx.send(
            f"❌ {owner.mention} only has **{owner_free_shares:,}** free shares of **{ticker}**. "
            f"Use `!grant @{owner.name} {ticker} [amount]` to issue more, or lower the IPO share count."
        )
        return

    new_owner_shares = owner_free_shares - ipo_shares
    if new_owner_shares == 0:
        cursor.execute("DELETE FROM portfolios WHERE discord_id = %s AND ticker = %s", (owner_id, ticker))
    else:
        cursor.execute("UPDATE portfolios SET shares = %s WHERE discord_id = %s AND ticker = %s",
                       (new_owner_shares, owner_id, ticker))

    cursor.execute(
        "UPDATE stocks SET ipo_price = %s, ipo_shares = %s, ipo_owner_id = %s WHERE ticker = %s",
        (ipo_price, ipo_shares, owner_id, ticker)
    )
    conn.commit()

    await ctx.send(
        f"🔓 **IPO Launched — {company_name} ({ticker})**\n"
        f"**{ipo_shares:,}** shares from {owner.mention} available at **${ipo_price:,.2f}** each.\n"
        f"Sale proceeds go directly to {owner.mention}. Players use `!buyipo {ticker} [shares]` to participate.\n"
        f"Use `!endipo {ticker}` to close it early."
    )

@bot.command()
async def endipo(ctx, ticker: str):
    """[Admin] End an active IPO, returning unsold shares to the original company owner. Usage: !endipo ABC"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    ticker = ticker.upper()

    # Crucial Fix: We now pull the 'ipo_owner_id' along with the other data
    cursor.execute("SELECT company_name, ipo_price, ipo_shares, ipo_owner_id FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** does not exist.")
        return

    company_name, ipo_price, ipo_shares, ipo_owner_id = row
    if not ipo_price or not ipo_shares or ipo_shares <= 0:
        await ctx.send(f"❌ **{ticker}** does not have an active IPO.")
        return
    if not ipo_owner_id:
        await ctx.send(f"❌ Error: Could not find the original owner for this IPO.")
        return

    # Return unsold IPO shares to the actual company owner, NOT the admin
    owner_shares = get_shares_owned(ipo_owner_id, ticker)
    if owner_shares > 0:
        cursor.execute("UPDATE portfolios SET shares = shares + %s WHERE discord_id = %s AND ticker = %s",
                       (ipo_shares, ipo_owner_id, ticker))
    else:
        cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (%s, %s, %s)",
                       (ipo_owner_id, ticker, ipo_shares))

    # Clear out the IPO data from the stock tracking
    cursor.execute("UPDATE stocks SET ipo_price = NULL, ipo_shares = 0, ipo_owner_id = NULL WHERE ticker = %s", (ticker,))
    conn.commit()

    await ctx.send(
        f"🔒 **IPO Closed — {company_name} ({ticker})**\n"
        f"**{ipo_shares:,}** unsold shares successfully returned to the company owner (<@{ipo_owner_id}>). Normal order-book trading continues."
    )

@bot.command()
async def activityreport(ctx, days: int = 7):
    """[Admin] Trade volume report. Usage: !activityreport 7"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("SELECT ticker, company_name FROM stocks")
    all_stocks = cursor.fetchall()
    if not all_stocks:
        await ctx.send("No stocks listed.")
        return

    embed = discord.Embed(
        title=f"📊 Activity Report — Last {days} Days",
        color=discord.Color.blue(),
        description="Completed trades and live order-book depth."
    )
    for ticker, name in all_stocks:
        cursor.execute("SELECT COUNT(*), COALESCE(SUM(shares), 0) FROM transactions WHERE ticker = %s AND type = 'buy' AND timestamp >= %s", (ticker, since))
        trade_count, vol = cursor.fetchone()
        cursor.execute("SELECT current_price, total_shares, auto_price FROM stocks WHERE ticker = %s", (ticker,))
        price, total, auto = cursor.fetchone()
        cursor.execute("SELECT COUNT(*), COALESCE(SUM(shares-filled),0) FROM orders WHERE ticker = %s AND type='buy' AND status IN ('open','partial')", (ticker,))
        buy_orders, buy_depth = cursor.fetchone()
        cursor.execute("SELECT COUNT(*), COALESCE(SUM(shares-filled),0) FROM orders WHERE ticker = %s AND type='sell' AND status IN ('open','partial')", (ticker,))
        sell_orders, sell_depth = cursor.fetchone()
        auto_tag = " 🤖" if auto else " 🔧"
        value = (
            f"Trades: **{trade_count}** | Vol: **{int(vol):,}** shares\n"
            f"Price: **${price:,.2f}**{auto_tag} | Supply: **{total:,}**\n"
            f"Bids: **{int(buy_orders)}** orders ({int(buy_depth):,} shares) | Asks: **{int(sell_orders)}** orders ({int(sell_depth):,} shares)"
        )
        embed.add_field(name=f"{name} ({ticker})", value=value, inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def vieworders(ctx, ticker: str):
    """[Admin] View all open orders for a stock. Usage: !vieworders ABC"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    ticker = ticker.upper()
    cursor.execute("""
        SELECT id, discord_id, type, shares, filled, price, placed_at
        FROM orders WHERE ticker = %s AND status IN ('open','partial')
        ORDER BY type ASC, price DESC, placed_at ASC
    """, (ticker,))
    rows = cursor.fetchall()

    if not rows:
        await ctx.send(f"No open orders for **{ticker}**.")
        return

    embed = discord.Embed(title=f"📋 Open Orders — {ticker}", color=discord.Color.orange())
    for oid, did, otype, tot, filled, price, placed_at in rows:
        remaining = tot - filled
        uname = await fetch_display_name(ctx.guild, did)
        date_str = placed_at[:10] if placed_at else "?"
        embed.add_field(
            name=f"#{oid} — {otype.upper()} by {uname} ({date_str})",
            value=f"{remaining:,} shares @ **${price:,.2f}**" + (f" | {filled:,} filled" if filled > 0 else ""),
            inline=False
        )
    await ctx.send(embed=embed)


@bot.command()
async def admincancel(ctx, order_id: int):
    """[Admin] Cancel any open order. Usage: !admincancel 42"""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    cursor.execute(
        "SELECT discord_id, ticker, type, shares, filled, price, status FROM orders WHERE id = %s", (order_id,)
    )
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ Order **#{order_id}** not found.")
        return

    did, ticker, otype, tot, filled, price, status = row
    if status not in ('open', 'partial'):
        await ctx.send(f"❌ Order **#{order_id}** is already **{status}**.")
        return

    remaining = tot - filled
    admin_cancel_fee = 0.0 if did == str(ADMIN_IDS[0]) else get_fee()
    cursor.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (order_id,))

    if otype == 'buy':
        refund = round(remaining * price * (1 + admin_cancel_fee), 2)
        cursor.execute("UPDATE users SET cash = cash + %s WHERE discord_id = %s", (refund, did))
        conn.commit()
        uname = await fetch_display_name(ctx.guild, did)
        await ctx.send(f"✅ Buy order **#{order_id}** cancelled. **${refund:,.2f}** returned to **{uname}**.")
    else:
        p = get_shares_owned(did, ticker)
        if p > 0:
            cursor.execute("UPDATE portfolios SET shares = shares + %s WHERE discord_id = %s AND ticker = %s",
                           (remaining, did, ticker))
        else:
            cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (%s, %s, %s)",
                           (did, ticker, remaining))
        conn.commit()
        uname = await fetch_display_name(ctx.guild, did)
        await ctx.send(f"✅ Sell order **#{order_id}** cancelled. **{remaining:,}** shares of **{ticker}** returned to **{uname}**.")


@bot.command()
async def fees(ctx):
    """[Admin] Show total fee income collected by the exchange, broken down by stock."""
    ensure_connection()
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    cursor.execute("SELECT ticker, COALESCE(SUM(amount), 0) FROM fee_income GROUP BY ticker ORDER BY SUM(amount) DESC")
    rows = cursor.fetchall()

    cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM fee_income")
    grand_total = cursor.fetchone()[0] or 0.0

    cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM fee_income WHERE timestamp >= %s",
                   ((datetime.utcnow() - timedelta(days=30)).isoformat(),))
    last_30 = cursor.fetchone()[0] or 0.0

    cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM fee_income WHERE timestamp >= %s",
                   ((datetime.utcnow() - timedelta(days=7)).isoformat(),))
    last_7 = cursor.fetchone()[0] or 0.0

    embed = discord.Embed(
        title="💰 Exchange Fee Income",
        color=discord.Color.gold(),
        description=(
            f"**All-time total:** ${grand_total:,.2f}\n"
            f"**Last 30 days:** ${last_30:,.2f}\n"
            f"**Last 7 days:** ${last_7:,.2f}"
        )
    )

    if rows:
        lines = []
        for ticker, total in rows:
            cursor.execute("SELECT company_name FROM stocks WHERE ticker = %s", (ticker,))
            name_row = cursor.fetchone()
            name = name_row[0] if name_row else ticker
            pct = (total / grand_total * 100) if grand_total > 0 else 0
            lines.append(f"**{name} ({ticker})** — ${total:,.2f} ({pct:.1f}%)")
        embed.add_field(name="By Stock", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="By Stock", value="No fee income recorded yet.", inline=False)

    embed.set_footer(text="Fees are collected from both sides of every completed trade and IPO purchase.")
    await ctx.send(embed=embed)


@bot.command()
async def shareholders(ctx, ticker: str):
    """List all current shareholders of a company. Usage: !shareholders ABC"""
    ensure_connection()
    ticker = ticker.upper()

    cursor.execute("SELECT company_name, total_shares FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** is not listed.")
        return

    company_name, total_shares = row

    # Shares held in portfolio
    cursor.execute(
        "SELECT discord_id, shares FROM portfolios WHERE ticker = %s AND shares > 0",
        (ticker,)
    )
    holders = {r[0]: r[1] for r in cursor.fetchall()}

    # Add shares locked in open sell orders (still owned by seller)
    cursor.execute(
        "SELECT discord_id, SUM(shares - filled) FROM orders "
        "WHERE ticker = %s AND type = 'sell' AND status IN ('open','partial') GROUP BY discord_id",
        (ticker,)
    )
    for discord_id, locked in cursor.fetchall():
        holders[discord_id] = holders.get(discord_id, 0) + int(locked)

    if not holders:
        await ctx.send(f"**{company_name} ({ticker})** has no current shareholders.")
        return

    sorted_holders = sorted(holders.items(), key=lambda x: x[1], reverse=True)

    embed = discord.Embed(
        title=f"🏢 Shareholders — {company_name} ({ticker})",
        description=f"Total shares in circulation: **{total_shares:,}**",
        color=discord.Color.teal()
    )

    lines = []
    for i, (discord_id, shares) in enumerate(sorted_holders):
        name = await fetch_display_name(ctx.guild, discord_id)
        pct = (shares / total_shares * 100) if total_shares > 0 else 0.0
        rank = f"**#{i+1}**"
        lines.append(f"{rank} **{name}** — {shares:,} shares ({pct:.1f}%)")

    embed.description += "\n\n" + "\n".join(lines)
    embed.set_footer(text="Includes shares locked in open sell orders. Use !dividend to pay out.")
    await ctx.send(embed=embed)


@bot.command()
async def chart(ctx, ticker: str, days: int = 60):
    """Show a real price chart image for a stock. Usage: !chart ABC or !chart ABC 30"""
    ensure_connection()
    ticker = ticker.upper()

    cursor.execute("SELECT company_name, current_price FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** is not listed.")
        return
    company_name, current_price = row

    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT price, timestamp FROM transactions
        WHERE ticker = %s AND type = 'buy' AND timestamp >= %s
        ORDER BY timestamp ASC
    """, (ticker, since))
    rows = cursor.fetchall()

    if len(rows) < 2:
        await ctx.send(
            f"📊 **{company_name} ({ticker})** — not enough trade data for a chart yet.\n"
            f"Current reference price: **${current_price:,.2f}**. Trades will appear here once they complete."
        )
        return

    prices = [r[0] for r in rows]
    timestamps = []
    for r in rows:
        try:
            timestamps.append(datetime.fromisoformat(r[1]))
        except (ValueError, TypeError):
            timestamps.append(datetime.utcnow())

    high_price = max(prices)
    low_price  = min(prices)
    open_price = prices[0]
    close_price = prices[-1]
    change_pct = ((close_price - open_price) / open_price * 100) if open_price > 0 else 0

    # ── Chart rendering ───────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor('#1a1a2e')
    ax.set_facecolor('#16213e')

    line_color = '#00e676' if close_price >= open_price else '#ff5252'
    fill_color = '#00e67622' if close_price >= open_price else '#ff525222'

    ax.plot(timestamps, prices, color=line_color, linewidth=2.0, zorder=3)
    ax.fill_between(timestamps, prices, min(prices) * 0.995, color=fill_color, zorder=2)

    ax.set_title(
        f"{company_name} ({ticker})   ${close_price:,.2f}   {'+' if change_pct >= 0 else ''}{change_pct:.1f}%",
        color='white', fontsize=13, fontweight='bold', pad=12
    )
    ax.set_xlabel(f"Last {days} days  |  {len(prices)} trades", color='#aaaaaa', fontsize=9)
    ax.set_ylabel("Price ($)", color='#aaaaaa', fontsize=9)

    ax.tick_params(colors='#aaaaaa', labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor('#333355')

    ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=30, ha='right')

    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'${v:,.2f}'))
    ax.grid(axis='y', color='#333355', linewidth=0.6, linestyle='--', alpha=0.7)

    # Stat box
    stats = f"High: ${high_price:,.2f}   Low: ${low_price:,.2f}   Open: ${open_price:,.2f}"
    ax.annotate(stats, xy=(0.01, 0.02), xycoords='axes fraction',
                color='#aaaaaa', fontsize=8,
                bbox=dict(boxstyle='round,pad=0.3', facecolor='#0f3460', edgecolor='#333355'))

    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=130, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)

    await ctx.send(
        file=discord.File(buf, filename=f"{ticker}_chart.png")
    )


@bot.command()
async def watch(ctx, ticker: str):
    """Add a stock to your watchlist. Usage: !watch ABC"""
    ensure_connection()
    ticker = ticker.upper()
    cursor.execute("SELECT company_name FROM stocks WHERE ticker = %s", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ **{ticker}** is not listed.")
        return
    try:
        cursor.execute(
            "INSERT INTO watchlists (discord_id, ticker) VALUES (%s, %s)",
            (str(ctx.author.id), ticker)
        )
        conn.commit()
        await ctx.send(f"👁️ **{row[0]} ({ticker})** added to your watchlist. Use `!watchlist` to see it.")
    except psycopg2.IntegrityError:
        conn.rollback()
        await ctx.send(f"**{ticker}** is already on your watchlist.")


@bot.command()
async def unwatch(ctx, ticker: str):
    """Remove a stock from your watchlist. Usage: !unwatch ABC"""
    ensure_connection()
    ticker = ticker.upper()
    cursor.execute(
        "DELETE FROM watchlists WHERE discord_id = %s AND ticker = %s",
        (str(ctx.author.id), ticker)
    )
    removed = cursor.rowcount
    conn.commit()
    if removed:
        await ctx.send(f"🗑️ **{ticker}** removed from your watchlist.")
    else:
        await ctx.send(f"**{ticker}** wasn't on your watchlist.")


@bot.command()
async def watchlist(ctx):
    """View your personal watchlist with prices and your position."""
    ensure_connection()
    uid = str(ctx.author.id)
    cursor.execute(
        "SELECT ticker FROM watchlists WHERE discord_id = %s ORDER BY ticker ASC",
        (uid,)
    )
    tickers = [r[0] for r in cursor.fetchall()]

    if not tickers:
        await ctx.send(
            "Your watchlist is empty. Use `!watch ABC` to add stocks to it."
        )
        return

    embed = discord.Embed(
        title=f"👁️ {ctx.author.name}'s Watchlist",
        color=discord.Color.blurple()
    )

    for ticker in tickers:
        cursor.execute(
            "SELECT company_name, current_price, previous_price, ipo_price, ipo_shares FROM stocks WHERE ticker = %s",
            (ticker,)
        )
        stock = cursor.fetchone()
        if not stock:
            embed.add_field(name=ticker, value="*(delisted)*", inline=False)
            continue

        name, price, prev_price, ipo_price, ipo_shares = stock

        cursor.execute(
            "SELECT MAX(price) FROM orders WHERE ticker = %s AND type = 'buy' AND status IN ('open','partial')",
            (ticker,)
        )
        best_bid = cursor.fetchone()[0]
        cursor.execute(
            "SELECT MIN(price) FROM orders WHERE ticker = %s AND type = 'sell' AND status IN ('open','partial')",
            (ticker,)
        )
        best_ask = cursor.fetchone()[0]

        change_str = ""
        if prev_price and prev_price > 0 and prev_price != price:
            pct = ((price - prev_price) / prev_price) * 100
            change_str = f" ({'📈' if pct > 0 else '📉'} {pct:+.1f}%)"

        bid_str = f"${best_bid:,.2f}" if best_bid else "—"
        ask_str = f"${best_ask:,.2f}" if best_ask else "—"

        position_str = ""
        held = get_total_shares_held(uid, ticker)
        if held > 0:
            position_str = f"\n📦 **Your position:** {held:,} shares ≈ ${held * price:,.2f}"

        ipo_str = ""
        if ipo_price and ipo_shares and ipo_shares > 0:
            ipo_str = f"\n🔓 **IPO Active** — {ipo_shares:,} @ ${ipo_price:,.2f} (`!buyipo {ticker}`)"

        value = (
            f"Last: **${price:,.2f}**{change_str} | Bid: **{bid_str}** | Ask: **{ask_str}**"
            f"{position_str}{ipo_str}"
        )
        embed.add_field(name=f"{name} ({ticker})", value=value, inline=False)

    embed.set_footer(text="!watch [TICKER] to add  •  !unwatch [TICKER] to remove")
    await ctx.send(embed=embed)


bot.run(os.environ['DISCORD_TOKEN'])
