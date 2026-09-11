"""
把模擬倉的目前持倉（positions.json）和歷史交易（outputs/trade_log.csv）
整理成一份可以直接用瀏覽器打開看的視覺化儀表板（outputs/dashboard.html）。

用法（在你自己電腦，pip install shioaji pandas requests之後）：
    python generate_dashboard.py
跑完打開 outputs/dashboard.html 就能看到目前庫存跟歷史交易的表格與統計。
"""
import os
import json
from datetime import datetime, timedelta

import pandas as pd

from tw_pattern_screener_v2 import (
    fetch_twse_day, fetch_tpex_day, _get_with_retry,
    TWSE_MI_INDEX_URL, TPEX_DAILY_URL,
)

POSITIONS_FILE = "positions.json"
TRADE_LOG_FILE = "outputs/trade_log.csv"
DASHBOARD_FILE = "outputs/dashboard.html"
STOCK_NAME_CACHE_FILE = "stock_names_cache.json"


def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_trade_log():
    if os.path.exists(TRADE_LOG_FILE):
        df = pd.read_csv(TRADE_LOG_FILE)
        if "股數" not in df.columns:
            df["股數"] = 1000  # 舊格式沒有記錄股數的交易，用整股1張回推（歷史上這些筆確實都是整股1張）
        return df
    return pd.DataFrame(columns=["股票代碼", "進場日期", "進場價", "出場日期", "出場價", "出場原因", "報酬率", "股數"])


def fetch_latest_prices(tickers):
    """往前找最近一個有資料的交易日，抓這些股票的最新收盤價"""
    if not tickers:
        return {}
    for i in range(10):
        d = datetime.now() - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        twse = fetch_twse_day(d)
        tpex = fetch_tpex_day(d)
        merged = {**twse, **tpex}
        if merged:
            return {t: merged[t]["close"] for t in tickers if t in merged}
    return {}


def _load_name_cache():
    if os.path.exists(STOCK_NAME_CACHE_FILE):
        try:
            with open(STOCK_NAME_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_name_cache(cache):
    with open(STOCK_NAME_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _find_name_field(fields):
    """不同端點欄位名稱可能略有不同，依序嘗試常見的名稱欄位"""
    for cand in ("證券名稱", "名稱", "股票名稱"):
        if cand in fields:
            return cand
    return None


def _fetch_twse_names(date):
    """抓上市股票某一天的代碼->名稱對照，回傳如 {'2330.TW': '台積電'}"""
    params = {"response": "json", "date": date.strftime("%Y%m%d"), "type": "ALLBUT0999"}
    data = _get_with_retry(TWSE_MI_INDEX_URL, params, is_json=True)
    if not data or data.get("stat") != "OK" or "tables" not in data:
        return {}
    target_table = None
    for t in data["tables"]:
        fs = t.get("fields") or []
        if "證券代號" in fs and "收盤價" in fs:
            target_table = t
            break
    if target_table is None:
        return {}
    fields = target_table["fields"]
    name_field = _find_name_field(fields)
    if not name_field:
        return {}
    idx_code, idx_name = fields.index("證券代號"), fields.index(name_field)
    result = {}
    for row in target_table["data"]:
        code = str(row[idx_code]).strip()
        if not code.isdigit() or len(code) != 4:
            continue
        result[f"{code}.TW"] = str(row[idx_name]).strip()
    return result


def _fetch_tpex_names(date):
    """抓上櫃股票某一天的代碼->名稱對照，回傳如 {'6488.TWO': '環球晶'}"""
    params = {"date": date.strftime("%Y/%m/%d"), "response": "json"}
    data = _get_with_retry(TPEX_DAILY_URL, params, is_json=True)
    if not data or "tables" not in data or not data["tables"]:
        return {}
    table = data["tables"][0]
    fields = table.get("fields") or []
    name_field = _find_name_field(fields)
    if "代號" not in fields or not name_field:
        return {}
    idx_code, idx_name = fields.index("代號"), fields.index(name_field)
    result = {}
    for row in table.get("data", []):
        try:
            code = str(row[idx_code]).strip()
        except Exception:
            continue
        if not code.isdigit() or len(code) != 4:
            continue
        result[f"{code}.TWO"] = str(row[idx_name]).strip()
    return result


def fetch_stock_names(tickers):
    """回傳 {股票代碼: 股票名稱}。名稱幾乎不會變，先查本地快取（stock_names_cache.json），
    只有快取沒有的代碼才去抓最近一個交易日的全市場代碼/名稱對照來補齊。"""
    if not tickers:
        return {}
    cache = _load_name_cache()
    missing = [t for t in tickers if t not in cache]
    if missing:
        for i in range(10):
            d = datetime.now() - timedelta(days=i)
            if d.weekday() >= 5:
                continue
            names = {}
            names.update(_fetch_twse_names(d))
            names.update(_fetch_tpex_names(d))
            if names:
                cache.update(names)
                _save_name_cache(cache)
                break
    return {t: cache.get(t, "") for t in tickers}


def fmt_quantity(shares):
    """股數顯示成好讀的格式：整千股顯示成「N張」，有零頭則顯示成「N股」（例如6500股->6.5張顯示會誤導，直接顯示股數）。"""
    shares = int(shares)
    if shares % 1000 == 0:
        return f"{shares // 1000}張"
    return f"{shares}股"


def build_holdings_rows(positions, latest_prices, stock_names=None):
    stock_names = stock_names or {}
    rows = []
    for ticker, pos in positions.items():
        cur = latest_prices.get(ticker)
        entry_price = pos["entry_price"]
        # quantity_shares是新格式（單位：股，零股／整股都適用）；quantity是舊格式相容（單位：張）
        shares = pos["quantity_shares"] if "quantity_shares" in pos else pos.get("quantity", 0) * 1000
        cost = entry_price * shares
        value = cur * shares if cur is not None else None
        pnl = (value - cost) if value is not None else None
        ret = (cur - entry_price) / entry_price if cur else None
        rows.append({
            "股票代碼": ticker, "股票名稱": stock_names.get(ticker, ""),
            "進場日期": pos["entry_date"], "進場價": entry_price,
            "現價": cur, "數量(張)": fmt_quantity(shares),
            "總成本": cost, "庫存現值": value, "未實現損益": pnl,
            "未實現報酬率": ret, "量能倍數": pos.get("量能倍數"),
        })
    return rows


def fmt_pct(x):
    if x is None or pd.isna(x):
        return "—"
    return f"{x*100:+.2f}%"


def fmt_money(x, signed=False):
    if x is None or pd.isna(x):
        return "—"
    if signed:
        return f"{x:+,.0f}"
    return f"{x:,.0f}"


def pct_class(x):
    if x is None or pd.isna(x):
        return ""
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def render_html(holdings_rows, trade_log_df, stock_names=None):
    stock_names = stock_names or {}
    closed = trade_log_df.dropna(subset=["報酬率"]) if not trade_log_df.empty else trade_log_df
    n_closed = len(closed)
    win_rate = (closed["報酬率"] > 0).mean() * 100 if n_closed else None
    avg_ret = closed["報酬率"].mean() if n_closed else None
    total_ret = closed["報酬率"].sum() if n_closed else None
    n_open = len(holdings_rows)

    # 已平倉的總成本／總出場金額／已實現損益（用股數*進場價/出場價回推，股數缺值時用1000股回推，
    # 對應load_trade_log()對舊格式資料的相容處理）
    if n_closed:
        closed_shares = closed["股數"].fillna(1000) if "股數" in closed.columns else 1000
        total_cost_all = float((closed["進場價"] * closed_shares).sum())
        total_proceeds_all = float((closed["出場價"] * closed_shares).sum())
        total_pnl_all = total_proceeds_all - total_cost_all
    else:
        total_cost_all = total_proceeds_all = total_pnl_all = 0.0

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    def stat_tile(label, value):
        return f'<div class="tile"><div class="tile-label">{label}</div><div class="tile-value">{value}</div></div>'

    tiles_html = "".join([
        stat_tile("目前持有檔數", n_open),
        stat_tile("已平倉交易筆數", n_closed),
        stat_tile("已平倉勝率", f"{win_rate:.1f}%" if win_rate is not None else "—"),
        stat_tile("已實現損益", fmt_money(total_pnl_all, signed=True) if n_closed else "—"),
    ])

    holdings_html = ""
    if holdings_rows:
        rows_html = ""
        for r in holdings_rows:
            ret_cls = pct_class(r["未實現報酬率"])
            pnl_cls = pct_class(r["未實現損益"])
            name = r.get("股票名稱") or stock_names.get(r["股票代碼"], "") or "—"
            rows_html += f"""<tr>
                <td>{r['股票代碼']}</td><td>{name}</td><td>{r['進場日期']}</td>
                <td>{r['進場價']}</td><td>{r['現價'] if r['現價'] is not None else '—'}</td>
                <td>{r['數量(張)']}</td>
                <td>{fmt_money(r['總成本'])}</td>
                <td>{fmt_money(r['庫存現值'])}</td>
                <td class="{pnl_cls}">{fmt_money(r['未實現損益'], signed=True)}</td>
                <td class="{ret_cls}">{fmt_pct(r['未實現報酬率'])}</td>
                <td>{r['量能倍數']}</td>
            </tr>"""
        holdings_html = f"""
        <table>
            <thead><tr><th>股票代碼</th><th>股票名稱</th><th>進場日期</th><th>進場價</th><th>現價</th>
            <th>數量</th><th>總成本</th><th>庫存現值</th><th>未實現損益</th>
            <th>未實現報酬率</th><th>進場量能倍數</th></tr></thead>
            <tbody>{rows_html}</tbody>
        </table>"""
    else:
        holdings_html = '<div class="empty">目前沒有持有任何模擬部位。</div>'

    trades_html = ""
    if not trade_log_df.empty:
        rows_html = ""
        total_cost = 0.0
        total_proceeds = 0.0
        for _, r in trade_log_df.sort_values("出場日期", ascending=False).iterrows():
            ret_cls = pct_class(r["報酬率"])
            name = stock_names.get(r["股票代碼"], "") or "—"
            shares = r["股數"] if pd.notna(r.get("股數")) else 1000
            cost = r["進場價"] * shares
            proceeds = r["出場價"] * shares
            pnl = proceeds - cost
            pnl_cls = pct_class(pnl)
            total_cost += cost
            total_proceeds += proceeds
            rows_html += f"""<tr>
                <td>{r['股票代碼']}</td><td>{name}</td><td>{r['進場日期']}</td><td>{r['進場價']}</td>
                <td>{r['出場日期']}</td><td>{r['出場價']}</td><td>{fmt_quantity(shares)}</td><td>{r['出場原因']}</td>
                <td>{fmt_money(cost)}</td><td>{fmt_money(proceeds)}</td>
                <td class="{pnl_cls}">{fmt_money(pnl, signed=True)}</td>
                <td class="{ret_cls}">{fmt_pct(r['報酬率'])}</td>
            </tr>"""
        total_pnl = total_proceeds - total_cost
        total_pnl_cls = pct_class(total_pnl)
        total_ret_all = (total_pnl / total_cost) if total_cost else None
        total_ret_cls = pct_class(total_ret_all)
        rows_html += f"""<tr class="total-row">
            <td colspan="8">總計（已實現，全部{n_closed}筆）</td>
            <td>{fmt_money(total_cost)}</td><td>{fmt_money(total_proceeds)}</td>
            <td class="{total_pnl_cls}">{fmt_money(total_pnl, signed=True)}</td>
            <td class="{total_ret_cls}">{fmt_pct(total_ret_all)}</td>
        </tr>"""
        trades_html = f"""
        <table>
            <thead><tr><th>股票代碼</th><th>股票名稱</th><th>進場日期</th><th>進場價</th>
            <th>出場日期</th><th>出場價</th><th>數量</th><th>出場原因</th>
            <th>總成本</th><th>出場金額</th><th>已實現損益</th><th>報酬率</th></tr></thead>
            <tbody>{rows_html}</tbody>
        </table>"""
    else:
        trades_html = '<div class="empty">目前還沒有任何已平倉的交易紀錄。</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>型態策略模擬倉儀表板</title>
<style>
  :root {{
    color-scheme: light;
    --surface-1: #fcfcfb; --surface-2: #f3f2ef; --border: #e3e2dd;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #7a7972;
    --good: #0ca30c; --critical: #d03b3b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --surface-1: #1a1a19; --surface-2: #232322; --border: #35342f;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #8f8e85;
      --good: #1fbf3a; --critical: #e66767;
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --surface-1: #1a1a19; --surface-2: #232322; --border: #35342f;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #8f8e85;
    --good: #1fbf3a; --critical: #e66767;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--surface-1); color: var(--text-primary); font-family: -apple-system, "Segoe UI", "PingFang TC", "Microsoft JhengHei", sans-serif; }}
  .viz-root {{ background: var(--surface-1); color: var(--text-primary); padding: 32px 24px; min-height: 100vh; }}
  .wrap {{ max-width: 920px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .updated {{ color: var(--text-muted); font-size: 13px; margin-bottom: 24px; }}
  .tiles {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 28px; }}
  .tile {{ background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
  .tile-label {{ font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }}
  .tile-value {{ font-size: 22px; font-weight: 600; }}
  h2 {{ font-size: 15px; margin: 28px 0 10px; color: var(--text-secondary); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--text-secondary); font-weight: 500; }}
  td.pos {{ color: var(--good); font-weight: 600; }}
  td.neg {{ color: var(--critical); font-weight: 600; }}
  tr.total-row td {{ font-weight: 600; border-top: 2px solid var(--border); border-bottom: none; }}
  .empty {{ color: var(--text-muted); font-size: 13px; padding: 16px 0; }}
  .note {{ margin-top: 28px; font-size: 12px; color: var(--text-muted); line-height: 1.6; }}
</style>
</head>
<body>
<div class="viz-root">
  <div class="wrap">
    <h1>連跌三日型態策略・模擬倉儀表板</h1>
    <div class="updated">更新時間：{now_str}（每次跑 tw_pattern_screener_shioaji.py 或 generate_dashboard.py 就會重新整理這個檔案）</div>
    <div class="tiles">{tiles_html}</div>
    <h2>目前持有部位</h2>
    {holdings_html}
    <h2>歷史交易紀錄（已平倉）</h2>
    {trades_html}
    <div class="note">
      這是純本地紙上模擬倉的記帳結果，不連任何券商、不涉及真實資金與真實成交。
      未實現報酬率是用最近一個交易日的收盤價概算，不是即時報價。
      本頁純粹是資料整理與統計呈現，不構成投資建議。
    </div>
  </div>
</div>
</body>
</html>"""


def main():
    os.makedirs("outputs", exist_ok=True)
    positions = load_positions()
    trade_log_df = load_trade_log()

    all_tickers = set(positions.keys()) | (
        set(trade_log_df["股票代碼"].tolist()) if not trade_log_df.empty else set()
    )
    stock_names = fetch_stock_names(list(all_tickers))

    latest_prices = fetch_latest_prices(list(positions.keys()))
    holdings_rows = build_holdings_rows(positions, latest_prices, stock_names)

    html = render_html(holdings_rows, trade_log_df, stock_names)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"儀表板已產生：{DASHBOARD_FILE}，用瀏覽器打開它就能看到目前庫存跟歷史交易。")


if __name__ == "__main__":
    main()
