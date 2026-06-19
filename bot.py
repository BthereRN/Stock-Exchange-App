
import discord
from discord.ext import commands, tasks
import sqlite3
import os
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread

# Lightweight web server to keep the bot alive on free hosting
app = Flask(__name__)

@app.route('/')
def home():
    return "Stoneworks Exchange Bot is running!"

def run_server():
    app.run(host='0.0.0.0', port=5000)

Thread(target=run_server, daemon=True).start()

# 1. Setup Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

ADMIN_IDS = [909889735038746694]

# 2. Setup SQLite Database Structure
conn = sqlite3.connect("stoneworks_exchange.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    discord_id TEXT PRIMARY KEY,
    cash REAL DEFAULT 0.0
)""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS stocks (
    ticker TEXT PRIMARY KEY,
    company_name TEXT,
    current_price REAL,
    total_shares INTEGER DEFAULT 0,
    available_shares INTEGER DEFAULT 0,
    company_networth REAL DEFAULT 0.0,
    auto_price INTEGER DEFAULT 1,
    last_price_update TEXT,
    previous_price REAL DEFAULT 0.0
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
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT,
    ticker TEXT,
    type TEXT,
    shares INTEGER,
    price REAL,
    timestamp TEXT
)""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS waitlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT,
    ticker TEXT,
    shares INTEGER,
    queued_at TEXT
)""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
)""")

conn.commit()

# Migrate existing tables if columns are missing
for col, col_type in [
    ("total_shares",     "INTEGER DEFAULT 0"),
    ("available_shares", "INTEGER DEFAULT 0"),
    ("company_networth", "REAL DEFAULT 0.0"),
    ("auto_price",       "INTEGER DEFAULT 1"),
    ("last_price_update","TEXT"),
    ("previous_price",   "REAL DEFAULT 0.0"),
]:
    try:
        cursor.execute(f"ALTER TABLE stocks ADD COLUMN {col} {col_type}")
        conn.commit()
    except sqlite3.OperationalError:
        pass

# Seed default config values
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('fee_percent', '2.0')")
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('price_channel_id', '')")
conn.commit()

# ── Helpers ──────────────────────────────────────────────────────────────────

def is_registered(discord_id):
    cursor.execute("SELECT cash FROM users WHERE discord_id = ?", (str(discord_id),))
    return cursor.fetchone() is not None

def get_config(key):
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row else None

def get_fee():
    try:
        return float(get_config("fee_percent")) / 100.0
    except (TypeError, ValueError):
        return 0.02

def credit_fee_to_admin(amount):
    """Add fee amount to the first admin's account (creates account if missing)."""
    admin_id = str(ADMIN_IDS[0])
    cursor.execute("SELECT cash FROM users WHERE discord_id = ?", (admin_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("UPDATE users SET cash = cash + ? WHERE discord_id = ?", (amount, admin_id))
    else:
        cursor.execute("INSERT INTO users (discord_id, cash) VALUES (?, ?)", (admin_id, amount))

async def process_waitlist(ticker, guild):
    cursor.execute("SELECT available_shares, current_price FROM stocks WHERE ticker = ?", (ticker,))
    stock = cursor.fetchone()
    if not stock:
        return

    available, price = stock
    fee_rate = get_fee()

    cursor.execute(
        "SELECT id, discord_id, shares FROM waitlist WHERE ticker = ? ORDER BY queued_at ASC",
        (ticker,)
    )
    queue = cursor.fetchall()

    for entry_id, discord_id, requested_shares in queue:
        if available <= 0:
            break

        can_fill = min(requested_shares, available)
        base_cost = price * can_fill
        fee = round(base_cost * fee_rate, 2)
        total_cost = base_cost + fee

        cursor.execute("SELECT cash FROM users WHERE discord_id = ?", (discord_id,))
        user = cursor.fetchone()
        if not user or user[0] < total_cost:
            continue

        cursor.execute("UPDATE users SET cash = cash - ? WHERE discord_id = ?", (total_cost, discord_id))
        cursor.execute("UPDATE stocks SET available_shares = available_shares - ? WHERE ticker = ?", (can_fill, ticker))
        credit_fee_to_admin(fee)

        cursor.execute("SELECT shares FROM portfolios WHERE discord_id = ? AND ticker = ?", (discord_id, ticker))
        p_row = cursor.fetchone()
        if p_row:
            cursor.execute("UPDATE portfolios SET shares = ? WHERE discord_id = ? AND ticker = ?", (p_row[0] + can_fill, discord_id, ticker))
        else:
            cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (?, ?, ?)", (discord_id, ticker, can_fill))

        cursor.execute(
            "INSERT INTO transactions (discord_id, ticker, type, shares, price, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (discord_id, ticker, "buy", can_fill, price, datetime.utcnow().isoformat())
        )

        if can_fill >= requested_shares:
            cursor.execute("DELETE FROM waitlist WHERE id = ?", (entry_id,))
        else:
            cursor.execute("UPDATE waitlist SET shares = shares - ? WHERE id = ?", (can_fill, entry_id))

        conn.commit()
        available -= can_fill

        member = guild.get_member(int(discord_id))
        if member:
            try:
                await member.send(
                    f"✅ **Waitlist Order Filled!** Your order for **{can_fill:,}** shares of **{ticker}** "
                    f"has been purchased at **${price:,.2f}** per share.\n"
                    f"Fee ({get_config('fee_percent')}%): **${fee:,.2f}** | Total charged: **${total_cost:,.2f}**."
                )
            except discord.Forbidden:
                pass

# ── Auto-price background task ────────────────────────────────────────────────

@tasks.loop(hours=1)
async def auto_price_task():
    now = datetime.utcnow()
    three_days_ago = (now - timedelta(days=3)).isoformat()

    cursor.execute("SELECT ticker, company_name, current_price, total_shares, last_price_update FROM stocks WHERE auto_price = 1")
    stocks = cursor.fetchall()

    price_channel_id = get_config("price_channel_id")
    channel = None
    if price_channel_id:
        try:
            channel = bot.get_channel(int(price_channel_id))
        except (ValueError, TypeError):
            pass

    for ticker, name, price, total, last_update in stocks:
        # Skip if updated less than 3 days ago
        if last_update:
            try:
                if datetime.fromisoformat(last_update) > now - timedelta(days=3):
                    continue
            except ValueError:
                pass

        # Calculate net demand over the past 3 days
        cursor.execute(
            "SELECT SUM(shares) FROM transactions WHERE ticker = ? AND type = 'buy' AND timestamp >= ?",
            (ticker, three_days_ago)
        )
        bought = cursor.fetchone()[0] or 0

        cursor.execute(
            "SELECT SUM(shares) FROM transactions WHERE ticker = ? AND type = 'sell' AND timestamp >= ?",
            (ticker, three_days_ago)
        )
        sold = cursor.fetchone()[0] or 0

        net_flow = bought - sold

        # If no trading activity at all, skip price change
        if bought == 0 and sold == 0:
            cursor.execute("UPDATE stocks SET last_price_update = ? WHERE ticker = ?", (now.isoformat(), ticker))
            conn.commit()
            continue

        # Demand ratio: net flow as % of total shares, sensitivity factor 0.5
        if total > 0:
            demand_ratio = (net_flow / total) * 0.5
        else:
            demand_ratio = 0

        # Cap change at ±25% per cycle
        demand_ratio = max(-0.25, min(0.25, demand_ratio))

        new_price = round(max(0.01, price * (1 + demand_ratio)), 2)

        cursor.execute(
            "UPDATE stocks SET current_price = ?, previous_price = ?, last_price_update = ? WHERE ticker = ?",
            (new_price, price, now.isoformat(), ticker)
        )
        conn.commit()

        print(f"[Auto-Price] {ticker}: ${price:.2f} → ${new_price:.2f} (bought {bought}, sold {sold})")

        if channel:
            direction = "📈" if new_price > price else "📉"
            pct = ((new_price - price) / price) * 100
            await channel.send(
                f"{direction} **Market Update — {name} ({ticker})**\n"
                f"Price adjusted from **${price:,.2f}** → **${new_price:,.2f}** ({pct:+.1f}%) "
                f"based on 3-day trading activity."
            )

# ── Bot Events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} - Stoneworks Custom Exchange Active!")
    auto_price_task.start()

# ── User Commands ─────────────────────────────────────────────────────────────

@bot.command(name="help")
async def help_command(ctx):
    """Show all available user commands."""
    embed = discord.Embed(
        title="📖 Stoneworks Exchange — Command Guide",
        description="Here are all the commands available to you:",
        color=discord.Color.green()
    )
    fee = get_config("fee_percent")
    embed.add_field(name="!market", value="View all listed stocks, prices, and share availability.", inline=False)
    embed.add_field(name="!balance", value="Check your cash, stocks owned, and total net worth.", inline=False)
    embed.add_field(name="!buy [TICKER] [amount]", value=f"Buy shares of a stock. A {fee}% exchange fee applies. Example: `!buy OBK 10`", inline=False)
    embed.add_field(name="!sell [TICKER] [amount]", value=f"Sell your shares back to the pool. A {fee}% exchange fee applies. Example: `!sell OBK 5`", inline=False)
    embed.add_field(name="!companyinfo [TICKER]", value="View detailed info about a listed company. Example: `!companyinfo OBK`", inline=False)
    embed.add_field(name="!joinwaitlist [TICKER] [amount]", value="Join the queue for a sold-out stock. Example: `!joinwaitlist OBK 10`", inline=False)
    embed.add_field(name="!waitlistpos [TICKER]", value="Check your position in a waitlist. Example: `!waitlistpos OBK`", inline=False)
    embed.add_field(name="!leavewaitlist [TICKER]", value="Remove yourself from a waitlist. Example: `!leavewaitlist OBK`", inline=False)
    embed.add_field(name="!leaderboard", value="See the top 10 wealthiest traders on the exchange.", inline=False)
    embed.set_footer(text="Need an account? Open a support ticket and send proof of your in-game payment.")
    await ctx.send(embed=embed)

@bot.command()
async def market(ctx):
    """List all available companies on the stock exchange."""
    cursor.execute("SELECT ticker, company_name, current_price, available_shares, total_shares, auto_price, previous_price FROM stocks")
    all_stocks = cursor.fetchall()

    embed = discord.Embed(title="🏛️ Stoneworks Stock Exchange", color=discord.Color.gold())

    if all_stocks:
        for ticker, name, price, available, total, auto, prev_price in all_stocks:
            auto_tag = " 🤖" if auto else ""
            if prev_price and prev_price > 0 and prev_price != price:
                pct = ((price - prev_price) / prev_price) * 100
                change_tag = f" ({'📈' if pct > 0 else '📉'} {pct:+.1f}%)"
            else:
                change_tag = ""
            if available > 0:
                status = f"Price: **${price:,.2f}**{change_tag}{auto_tag} | Available: **{available:,} / {total:,} shares**"
            else:
                status = f"Price: **${price:,.2f}**{change_tag}{auto_tag} | ⚠️ **SOLD OUT** (0 / {total:,} shares) — use `!joinwaitlist {ticker} <amount>`"
            embed.add_field(name=f"{name} ({ticker})", value=status, inline=False)
    else:
        embed.description = "The market is currently empty."

    fee = get_config("fee_percent")
    embed.set_footer(text=f"A {fee}% exchange fee applies to all buys and sells. 🤖 = auto-priced.")
    await ctx.send(embed=embed)

@bot.command()
async def balance(ctx):
    """Check your cash and stock holdings."""
    if not is_registered(ctx.author.id):
        await ctx.send("❌ You do not have an active exchange account. Please open a support ticket, send your in-game payment screenshot, and an admin will open your account!")
        return

    cursor.execute("SELECT cash FROM users WHERE discord_id = ?", (str(ctx.author.id),))
    cash = cursor.fetchone()[0]

    cursor.execute("""
        SELECT p.ticker, p.shares, s.current_price
        FROM portfolios p
        JOIN stocks s ON p.ticker = s.ticker
        WHERE p.discord_id = ? AND p.shares > 0
    """, (str(ctx.author.id),))
    holdings = cursor.fetchall()

    embed = discord.Embed(title=f"💼 {ctx.author.name}'s Portfolio", color=discord.Color.blue())
    embed.add_field(name="Available Balance", value=f"${cash:,.2f}", inline=False)

    if holdings:
        total_value = cash
        holdings_str = ""
        for ticker, shares, price in holdings:
            value = shares * price
            total_value += value
            holdings_str += f"**{ticker}**: {shares:,} shares (Worth: ${value:,.2f})\n"
        embed.add_field(name="Stocks Owned", value=holdings_str, inline=False)
        embed.add_field(name="Total Net Worth", value=f"${total_value:,.2f}", inline=False)
    else:
        embed.add_field(name="Stocks Owned", value="None.", inline=False)

    await ctx.send(embed=embed)

@bot.command()
async def buy(ctx, ticker: str, shares: int):
    """Buy shares of a stock. Usage: !buy OBK 10"""
    if not is_registered(ctx.author.id):
        await ctx.send("❌ You do not have an account yet.")
        return
    if shares <= 0:
        await ctx.send("❌ Quantity must be greater than 0.")
        return

    ticker = ticker.upper()

    cursor.execute("SELECT current_price, available_shares FROM stocks WHERE ticker = ?", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ Ticker **{ticker}** does not exist.")
        return

    price, available = row

    if available <= 0:
        await ctx.send(
            f"❌ **{ticker}** is sold out. Use `!joinwaitlist {ticker} {shares}` to get in line — "
            f"you'll be automatically filled when shares become available."
        )
        return

    if shares > available:
        await ctx.send(
            f"❌ Only **{available:,}** shares of **{ticker}** are available. "
            f"You can buy up to {available:,}, or use `!joinwaitlist {ticker} {shares}` to wait for the full amount."
        )
        return

    fee_rate = get_fee()
    base_cost = price * shares
    fee = round(base_cost * fee_rate, 2)
    total_cost = base_cost + fee

    cursor.execute("SELECT cash FROM users WHERE discord_id = ?", (str(ctx.author.id),))
    cash = cursor.fetchone()[0]

    if cash < total_cost:
        await ctx.send(
            f"❌ Insufficient funds. Cost: **${base_cost:,.2f}** + fee **${fee:,.2f}** = **${total_cost:,.2f}**. "
            f"You have **${cash:,.2f}**."
        )
        return

    cursor.execute("UPDATE users SET cash = cash - ? WHERE discord_id = ?", (total_cost, str(ctx.author.id)))
    cursor.execute("UPDATE stocks SET available_shares = available_shares - ? WHERE ticker = ?", (shares, ticker))
    credit_fee_to_admin(fee)

    cursor.execute("SELECT shares FROM portfolios WHERE discord_id = ? AND ticker = ?", (str(ctx.author.id), ticker))
    p_row = cursor.fetchone()
    if p_row:
        cursor.execute("UPDATE portfolios SET shares = ? WHERE discord_id = ? AND ticker = ?", (p_row[0] + shares, str(ctx.author.id), ticker))
    else:
        cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (?, ?, ?)", (str(ctx.author.id), ticker, shares))

    cursor.execute(
        "INSERT INTO transactions (discord_id, ticker, type, shares, price, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (str(ctx.author.id), ticker, "buy", shares, price, datetime.utcnow().isoformat())
    )

    conn.commit()
    await ctx.send(
        f"✅ Bought **{shares:,}** shares of **{ticker}** for **${base_cost:,.2f}** "
        f"+ {get_config('fee_percent')}% fee (**${fee:,.2f}**) = **${total_cost:,.2f}** total."
    )

@bot.command()
async def sell(ctx, ticker: str, shares: int):
    """Sell shares back to the pool. Usage: !sell OBK 5"""
    if not is_registered(ctx.author.id):
        await ctx.send("❌ You do not have an account yet.")
        return
    if shares <= 0:
        await ctx.send("❌ Quantity must be greater than 0.")
        return

    ticker = ticker.upper()

    cursor.execute("SELECT shares FROM portfolios WHERE discord_id = ? AND ticker = ?", (str(ctx.author.id), ticker))
    p_row = cursor.fetchone()
    if not p_row or p_row[0] < shares:
        await ctx.send(f"❌ You do not own enough shares of **{ticker}**.")
        return

    cursor.execute("SELECT current_price FROM stocks WHERE ticker = ?", (ticker,))
    price = cursor.fetchone()[0]

    fee_rate = get_fee()
    gross = price * shares
    fee = round(gross * fee_rate, 2)
    payout = gross - fee

    new_shares = p_row[0] - shares
    if new_shares == 0:
        cursor.execute("DELETE FROM portfolios WHERE discord_id = ? AND ticker = ?", (str(ctx.author.id), ticker))
    else:
        cursor.execute("UPDATE portfolios SET shares = ? WHERE discord_id = ? AND ticker = ?", (new_shares, str(ctx.author.id), ticker))

    cursor.execute("UPDATE stocks SET available_shares = available_shares + ? WHERE ticker = ?", (shares, ticker))
    cursor.execute("UPDATE users SET cash = cash + ? WHERE discord_id = ?", (payout, str(ctx.author.id)))
    credit_fee_to_admin(fee)

    cursor.execute(
        "INSERT INTO transactions (discord_id, ticker, type, shares, price, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (str(ctx.author.id), ticker, "sell", shares, price, datetime.utcnow().isoformat())
    )

    conn.commit()
    await ctx.send(
        f"💸 Sold **{shares:,}** shares of **{ticker}** for **${gross:,.2f}** "
        f"− {get_config('fee_percent')}% fee (**${fee:,.2f}**) = **${payout:,.2f}** received."
    )

    await process_waitlist(ticker, ctx.guild)

@bot.command()
async def companyinfo(ctx, ticker: str):
    """View detailed info about a listed company. Usage: !companyinfo OBK"""
    ticker = ticker.upper()

    cursor.execute("""
        SELECT company_name, current_price, total_shares, available_shares, company_networth, auto_price, last_price_update
        FROM stocks WHERE ticker = ?
    """, (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ Ticker **{ticker}** does not exist.")
        return

    name, price, total, available, networth, auto, last_update = row
    held_publicly = total - available
    market_cap = total * price

    embed = discord.Embed(title=f"🏢 {name} ({ticker})", color=discord.Color.teal())
    embed.add_field(name="Share Price", value=f"${price:,.2f}", inline=True)
    embed.add_field(name="Market Cap", value=f"${market_cap:,.2f}", inline=True)
    if networth and networth > 0:
        embed.add_field(name="Company Net Worth", value=f"${networth:,.2f}", inline=True)
    embed.add_field(name="Total Shares", value=f"{total:,}", inline=True)
    embed.add_field(name="Available in Pool", value=f"{available:,}", inline=True)
    embed.add_field(name="Shares Held", value=f"{held_publicly:,}", inline=True)

    cursor.execute("SELECT discord_id, shares FROM portfolios WHERE ticker = ? ORDER BY shares DESC LIMIT 1", (ticker,))
    top_holder = cursor.fetchone()
    if top_holder:
        member = ctx.guild.get_member(int(top_holder[0]))
        holder_name = member.display_name if member else f"User {top_holder[0]}"
        pct = (top_holder[1] / total * 100) if total > 0 else 0
        embed.add_field(name="Largest Shareholder", value=f"{holder_name} ({top_holder[1]:,} shares — {pct:.1f}%)", inline=False)

    pricing_mode = "🤖 Auto (3-day cycle)"
    if last_update:
        try:
            next_update = datetime.fromisoformat(last_update) + timedelta(days=3)
            days_left = max(0, (next_update - datetime.utcnow()).days)
            pricing_mode += f" — next update in ~{days_left}d"
        except ValueError:
            pass
    if not auto:
        pricing_mode = "🔧 Manual only"
    embed.add_field(name="Pricing Mode", value=pricing_mode, inline=False)

    status = "⚠️ SOLD OUT" if available == 0 else f"✅ {available:,} shares available"
    embed.set_footer(text=status)
    await ctx.send(embed=embed)

@bot.command()
async def joinwaitlist(ctx, ticker: str, shares: int):
    """Join the waitlist for a sold-out stock. Usage: !joinwaitlist OBK 10"""
    if not is_registered(ctx.author.id):
        await ctx.send("❌ You do not have an account yet.")
        return
    if shares <= 0:
        await ctx.send("❌ Quantity must be greater than 0.")
        return

    ticker = ticker.upper()

    cursor.execute("SELECT company_name, available_shares, current_price FROM stocks WHERE ticker = ?", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ Ticker **{ticker}** does not exist.")
        return

    name, available, price = row

    if available >= shares:
        await ctx.send(f"✅ **{ticker}** has shares available right now! Use `!buy {ticker} {shares}` to purchase them directly.")
        return

    cursor.execute("SELECT id FROM waitlist WHERE discord_id = ? AND ticker = ?", (str(ctx.author.id), ticker))
    existing = cursor.fetchone()
    if existing:
        cursor.execute("UPDATE waitlist SET shares = ? WHERE discord_id = ? AND ticker = ?", (shares, str(ctx.author.id), ticker))
        conn.commit()
        await ctx.send(f"🔄 Updated your waitlist order for **{ticker}** to **{shares:,}** shares.")
        return

    cursor.execute(
        "INSERT INTO waitlist (discord_id, ticker, shares, queued_at) VALUES (?, ?, ?, ?)",
        (str(ctx.author.id), ticker, shares, datetime.utcnow().isoformat())
    )
    conn.commit()

    cursor.execute("SELECT COUNT(*) FROM waitlist WHERE ticker = ?", (ticker,))
    position = cursor.fetchone()[0]

    await ctx.send(
        f"⏳ You're **#{position}** in line for **{shares:,}** shares of **{name} ({ticker})** "
        f"at **${price:,.2f}** each. You'll be notified via DM when your order is filled!"
    )

@bot.command()
async def waitlistpos(ctx, ticker: str):
    """Check your position in a stock's waitlist. Usage: !waitlistpos OBK"""
    ticker = ticker.upper()

    cursor.execute("SELECT id, shares FROM waitlist WHERE discord_id = ? AND ticker = ?", (str(ctx.author.id), ticker))
    entry = cursor.fetchone()
    if not entry:
        await ctx.send(f"You are not on the waitlist for **{ticker}**.")
        return

    entry_id, shares = entry
    cursor.execute(
        "SELECT COUNT(*) FROM waitlist WHERE ticker = ? AND queued_at <= (SELECT queued_at FROM waitlist WHERE id = ?)",
        (ticker, entry_id)
    )
    position = cursor.fetchone()[0]
    cursor.execute("SELECT current_price FROM stocks WHERE ticker = ?", (ticker,))
    price = cursor.fetchone()[0]

    fee_rate = get_fee()
    est_cost = shares * price * (1 + fee_rate)
    await ctx.send(
        f"📋 You are **#{position}** in the **{ticker}** waitlist for **{shares:,}** shares "
        f"(Est. cost incl. fee: **${est_cost:,.2f}**)."
    )

@bot.command()
async def leavewaitlist(ctx, ticker: str):
    """Leave the waitlist for a stock. Usage: !leavewaitlist OBK"""
    ticker = ticker.upper()
    cursor.execute("DELETE FROM waitlist WHERE discord_id = ? AND ticker = ?", (str(ctx.author.id), ticker))
    if cursor.rowcount > 0:
        conn.commit()
        await ctx.send(f"✅ You have been removed from the **{ticker}** waitlist.")
    else:
        await ctx.send(f"You were not on the waitlist for **{ticker}**.")

@bot.command()
async def leaderboard(ctx):
    """Show the top 10 users ranked by total net worth."""
    cursor.execute("SELECT discord_id, cash FROM users")
    all_users = cursor.fetchall()

    if not all_users:
        await ctx.send("No accounts exist yet.")
        return

    net_worths = []
    for discord_id, cash in all_users:
        cursor.execute("""
            SELECT p.shares, s.current_price
            FROM portfolios p
            JOIN stocks s ON p.ticker = s.ticker
            WHERE p.discord_id = ? AND p.shares > 0
        """, (discord_id,))
        holdings = cursor.fetchall()
        stock_value = sum(s * p for s, p in holdings)
        net_worths.append((discord_id, cash + stock_value))

    net_worths.sort(key=lambda x: x[1], reverse=True)
    top_10 = net_worths[:10]

    embed = discord.Embed(title="🏆 Stoneworks Wealth Leaderboard", color=discord.Color.gold())
    medals = ["🥇", "🥈", "🥉"]
    for i, (discord_id, net_worth) in enumerate(top_10):
        member = ctx.guild.get_member(int(discord_id))
        name = member.display_name if member else f"User {discord_id}"
        prefix = medals[i] if i < 3 else f"**#{i+1}**"
        embed.add_field(name=f"{prefix} {name}", value=f"${net_worth:,.2f}", inline=False)

    await ctx.send(embed=embed)

# ── Admin Commands ────────────────────────────────────────────────────────────

@bot.command()
async def deposit(ctx, member: discord.Member, amount: float):
    """[Admin] Create an account or fund a verified deposit. Usage: !deposit @Username 5000"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Only the exchange owner can process deposits.")
        return
    if amount <= 0:
        await ctx.send("❌ Amount must be positive.")
        return

    cursor.execute("SELECT cash FROM users WHERE discord_id = ?", (str(member.id),))
    row = cursor.fetchone()

    if row:
        new_balance = row[0] + amount
        cursor.execute("UPDATE users SET cash = ? WHERE discord_id = ?", (new_balance, str(member.id)))
        await ctx.send(f"💰 **Deposit Approved!** Added **${amount:,.2f}** to {member.mention}'s account. New Balance: **${new_balance:,.2f}**.")
    else:
        cursor.execute("INSERT INTO users (discord_id, cash) VALUES (?, ?)", (str(member.id), amount))
        await ctx.send(f"🏛️ **Account Created!** {member.mention} has been added to the exchange with a starting balance of **${amount:,.2f}**.")

    conn.commit()

@bot.command()
async def withdraw(ctx, member: discord.Member, amount: float):
    """[Admin] Deduct funds from a user's account. Usage: !withdraw @Username 1000"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Only the exchange owner can process withdrawals.")
        return
    if amount <= 0:
        await ctx.send("❌ Amount must be positive.")
        return

    cursor.execute("SELECT cash FROM users WHERE discord_id = ?", (str(member.id),))
    row = cursor.fetchone()

    if not row:
        await ctx.send(f"❌ {member.mention} does not have an account.")
        return
    if row[0] < amount:
        await ctx.send(f"❌ Insufficient funds. {member.mention} only has **${row[0]:,.2f}**.")
        return

    new_balance = row[0] - amount
    cursor.execute("UPDATE users SET cash = ? WHERE discord_id = ?", (new_balance, str(member.id)))
    conn.commit()
    await ctx.send(f"🏦 **Withdrawal Processed!** Deducted **${amount:,.2f}** from {member.mention}'s account. New Balance: **${new_balance:,.2f}**.")

@bot.command()
async def portfolio(ctx, member: discord.Member):
    """[Admin] View any user's balance and holdings. Usage: !portfolio @Username"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    cursor.execute("SELECT cash FROM users WHERE discord_id = ?", (str(member.id),))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ {member.mention} does not have an account.")
        return

    cash = row[0]
    cursor.execute("""
        SELECT p.ticker, p.shares, s.current_price
        FROM portfolios p
        JOIN stocks s ON p.ticker = s.ticker
        WHERE p.discord_id = ? AND p.shares > 0
    """, (str(member.id),))
    holdings = cursor.fetchall()

    embed = discord.Embed(title=f"🔍 {member.name}'s Portfolio (Admin View)", color=discord.Color.red())
    embed.add_field(name="Available Balance", value=f"${cash:,.2f}", inline=False)

    if holdings:
        total_value = cash
        holdings_str = ""
        for ticker, shares, price in holdings:
            value = shares * price
            total_value += value
            holdings_str += f"**{ticker}**: {shares:,} shares (Worth: ${value:,.2f})\n"
        embed.add_field(name="Stocks Owned", value=holdings_str, inline=False)
        embed.add_field(name="Total Net Worth", value=f"${total_value:,.2f}", inline=False)
    else:
        embed.add_field(name="Stocks Owned", value="None.", inline=False)

    await ctx.send(embed=embed)

@bot.command()
async def grant(ctx, member: discord.Member, ticker: str, shares: int):
    """[Admin] Grant founder shares to a user without drawing from the public pool. Usage: !grant @Username OBK 20"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if shares <= 0:
        await ctx.send("❌ Shares must be greater than 0.")
        return

    ticker = ticker.upper()
    cursor.execute("SELECT company_name, current_price FROM stocks WHERE ticker = ?", (ticker,))
    stock = cursor.fetchone()
    if not stock:
        await ctx.send(f"❌ Ticker **{ticker}** does not exist.")
        return

    company_name, price = stock

    if not is_registered(member.id):
        await ctx.send(f"❌ {member.mention} does not have an exchange account. Use `!deposit` to create one first.")
        return

    cursor.execute("SELECT shares FROM portfolios WHERE discord_id = ? AND ticker = ?", (str(member.id), ticker))
    p_row = cursor.fetchone()
    if p_row:
        cursor.execute("UPDATE portfolios SET shares = ? WHERE discord_id = ? AND ticker = ?", (p_row[0] + shares, str(member.id), ticker))
    else:
        cursor.execute("INSERT INTO portfolios (discord_id, ticker, shares) VALUES (?, ?, ?)", (str(member.id), ticker, shares))

    cursor.execute("UPDATE stocks SET total_shares = total_shares + ? WHERE ticker = ?", (shares, ticker))
    conn.commit()
    await ctx.send(
        f"🎟️ **Shares Granted!** {member.mention} has received **{shares:,} founder shares** of **{company_name} ({ticker})**. "
        f"Estimated value: **${shares * price:,.2f}**. These did not come from the public pool."
    )

@bot.command()
async def addstock(ctx, ticker: str, price: float, total_shares: int, *, name: str):
    """[Admin] List a new company. Usage: !addstock OBK 15.5 100 Obelisk Industries"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if total_shares <= 0:
        await ctx.send("❌ Total shares must be greater than 0.")
        return

    ticker = ticker.upper()
    now = datetime.utcnow().isoformat()
    try:
        cursor.execute(
            "INSERT INTO stocks (ticker, company_name, current_price, previous_price, total_shares, available_shares, auto_price, last_price_update) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (ticker, name, price, price, total_shares, total_shares, now)
        )
        conn.commit()
        await ctx.send(f"🏢 Listed **{name} ({ticker})** at **${price:,.2f}** per share with **{total_shares:,}** total shares. Auto-pricing enabled.")
    except sqlite3.IntegrityError:
        await ctx.send(f"❌ Ticker **{ticker}** already exists.")

@bot.command()
async def addshares(ctx, ticker: str, amount: int):
    """[Admin] Add more shares to a company's pool. Usage: !addshares OBK 50"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if amount <= 0:
        await ctx.send("❌ Amount must be greater than 0.")
        return

    ticker = ticker.upper()
    cursor.execute("SELECT company_name, total_shares, available_shares FROM stocks WHERE ticker = ?", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ Ticker **{ticker}** does not exist.")
        return

    name, total, available = row
    cursor.execute(
        "UPDATE stocks SET total_shares = total_shares + ?, available_shares = available_shares + ? WHERE ticker = ?",
        (amount, amount, ticker)
    )
    conn.commit()
    await ctx.send(f"📈 Added **{amount:,}** new shares to **{name} ({ticker})**. Total supply: **{total + amount:,}** ({available + amount:,} available).")
    await process_waitlist(ticker, ctx.guild)

@bot.command()
async def removestock(ctx, ticker: str):
    """[Admin] Delist a company and liquidate all holders. Usage: !removestock OBK"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    ticker = ticker.upper()
    cursor.execute("SELECT company_name, current_price FROM stocks WHERE ticker = ?", (ticker,))
    stock = cursor.fetchone()
    if not stock:
        await ctx.send(f"❌ Ticker **{ticker}** does not exist.")
        return

    company_name, price = stock

    cursor.execute("SELECT discord_id, shares FROM portfolios WHERE ticker = ?", (ticker,))
    holders = cursor.fetchall()

    for discord_id, shares in holders:
        cursor.execute("UPDATE users SET cash = cash + ? WHERE discord_id = ?", (shares * price, discord_id))
        cursor.execute("DELETE FROM portfolios WHERE discord_id = ? AND ticker = ?", (discord_id, ticker))

    cursor.execute("DELETE FROM waitlist WHERE ticker = ?", (ticker,))
    cursor.execute("DELETE FROM stocks WHERE ticker = ?", (ticker,))
    conn.commit()

    msg = f"🗑️ **{company_name} ({ticker})** has been delisted."
    if holders:
        msg += f" **{len(holders)}** shareholder(s) were paid out at **${price:,.2f}** per share."
    else:
        msg += " No shareholders were affected."
    await ctx.send(msg)

@bot.command()
async def setprice(ctx, ticker: str, new_price: float):
    """[Admin] Manually set a stock price and reset the auto-price timer. Usage: !setprice OBK 22.0"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    ticker = ticker.upper()
    cursor.execute("SELECT current_price FROM stocks WHERE ticker = ?", (ticker,))
    if not cursor.fetchone():
        await ctx.send("❌ Ticker not found.")
        return

    now = datetime.utcnow().isoformat()
    cursor.execute("SELECT current_price FROM stocks WHERE ticker = ?", (ticker,))
    old_price = cursor.fetchone()[0]
    cursor.execute("UPDATE stocks SET current_price = ?, previous_price = ?, last_price_update = ? WHERE ticker = ?", (new_price, old_price, now, ticker))
    conn.commit()
    await ctx.send(f"🔧 **{ticker}** manually set to **${new_price:,.2f}**. Auto-price timer has been reset.")

@bot.command()
async def setnetworth(ctx, ticker: str, networth: float):
    """[Admin] Set a company's in-game net worth. Usage: !setnetworth OBK 50000"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if networth < 0:
        await ctx.send("❌ Net worth cannot be negative.")
        return

    ticker = ticker.upper()
    cursor.execute("SELECT company_name FROM stocks WHERE ticker = ?", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ Ticker **{ticker}** does not exist.")
        return

    cursor.execute("UPDATE stocks SET company_networth = ? WHERE ticker = ?", (networth, ticker))
    conn.commit()
    await ctx.send(f"📋 **{row[0]} ({ticker})** net worth set to **${networth:,.2f}**.")

@bot.command()
async def autoprice(ctx, ticker: str, setting: str):
    """[Admin] Enable or disable auto-pricing for a stock. Usage: !autoprice OBK on/off"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    ticker = ticker.upper()
    setting = setting.lower()
    if setting not in ("on", "off"):
        await ctx.send("❌ Setting must be **on** or **off**.")
        return

    cursor.execute("SELECT company_name FROM stocks WHERE ticker = ?", (ticker,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"❌ Ticker **{ticker}** does not exist.")
        return

    val = 1 if setting == "on" else 0
    cursor.execute("UPDATE stocks SET auto_price = ? WHERE ticker = ?", (val, ticker))
    conn.commit()
    status = "🤖 enabled" if val else "🔧 disabled (manual only)"
    await ctx.send(f"Auto-pricing for **{row[0]} ({ticker})** is now {status}.")

@bot.command()
async def setfee(ctx, percent: float):
    """[Admin] Set the exchange fee percentage for buys and sells. Usage: !setfee 3.0"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return
    if percent < 0 or percent > 20:
        await ctx.send("❌ Fee must be between 0% and 20%.")
        return

    cursor.execute("UPDATE config SET value = ? WHERE key = 'fee_percent'", (str(percent),))
    conn.commit()
    await ctx.send(f"💱 Exchange fee updated to **{percent}%**. Applies to all future buys and sells.")

@bot.command()
async def setpricechannel(ctx, channel: discord.TextChannel):
    """[Admin] Set the channel where auto-price updates are announced. Usage: !setpricechannel #channel"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    cursor.execute("UPDATE config SET value = ? WHERE key = 'price_channel_id'", (str(channel.id),))
    conn.commit()
    await ctx.send(f"📢 Auto-price announcements will now be posted in {channel.mention}.")

@bot.command()
async def activityreport(ctx, days: int = 7):
    """[Admin] View buy/sell volume per stock over the last N days. Usage: !activityreport 7"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("SELECT ticker, company_name FROM stocks")
    all_stocks = cursor.fetchall()

    if not all_stocks:
        await ctx.send("No stocks are listed.")
        return

    embed = discord.Embed(
        title=f"📊 Activity Report — Last {days} Days",
        color=discord.Color.blue(),
        description="Shares bought and sold per company."
    )

    for ticker, name in all_stocks:
        cursor.execute("SELECT SUM(shares) FROM transactions WHERE ticker = ? AND type = 'buy' AND timestamp >= ?", (ticker, since))
        bought = cursor.fetchone()[0] or 0
        cursor.execute("SELECT SUM(shares) FROM transactions WHERE ticker = ? AND type = 'sell' AND timestamp >= ?", (ticker, since))
        sold = cursor.fetchone()[0] or 0
        cursor.execute("SELECT available_shares, total_shares, current_price, auto_price FROM stocks WHERE ticker = ?", (ticker,))
        avail, total, price, auto = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM waitlist WHERE ticker = ?", (ticker,))
        waitlist_count = cursor.fetchone()[0]

        net = bought - sold
        trend = "📈" if net > 0 else ("📉" if net < 0 else "➡️")
        auto_tag = " 🤖" if auto else " 🔧"
        value = (
            f"Bought: **{bought:,}** | Sold: **{sold:,}** | Net: **{net:+,}** {trend}\n"
            f"Pool: **{avail:,} / {total:,}** | Price: **${price:,.2f}**{auto_tag}"
        )
        if waitlist_count > 0:
            value += f"\n⏳ **{waitlist_count}** on waitlist"

        embed.add_field(name=f"{name} ({ticker})", value=value, inline=False)

    await ctx.send(embed=embed)

@bot.command()
async def viewwaitlist(ctx, ticker: str):
    """[Admin] See the full waitlist for a stock. Usage: !viewwaitlist OBK"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    ticker = ticker.upper()
    cursor.execute("SELECT discord_id, shares, queued_at FROM waitlist WHERE ticker = ? ORDER BY queued_at ASC", (ticker,))
    queue = cursor.fetchall()

    if not queue:
        await ctx.send(f"No one is on the waitlist for **{ticker}**.")
        return

    embed = discord.Embed(title=f"⏳ Waitlist for {ticker}", color=discord.Color.orange())
    for i, (discord_id, shares, queued_at) in enumerate(queue, 1):
        member = ctx.guild.get_member(int(discord_id))
        name = member.display_name if member else f"User {discord_id}"
        embed.add_field(name=f"#{i} — {name}", value=f"{shares:,} shares (since {queued_at[:10]})", inline=False)

    await ctx.send(embed=embed)

@bot.command()
async def clearwaitlist(ctx, ticker: str):
    """[Admin] Clear the waitlist for a stock. Usage: !clearwaitlist OBK"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("❌ Denied.")
        return

    ticker = ticker.upper()
    cursor.execute("DELETE FROM waitlist WHERE ticker = ?", (ticker,))
    conn.commit()
    await ctx.send(f"🗑️ Waitlist for **{ticker}** cleared.")

bot.run(os.environ['DISCORD_TOKEN'])

