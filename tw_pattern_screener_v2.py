"""
台股「連跌三日＋大紅K吃前高＋爆量」型態篩選腳本（v2：不依賴Yahoo Finance）
================================================================
背景：
    v1版用yfinance抓歷史OHLCV，但雲端執行環境（Cowork沙盒）的IP會被
    Yahoo Finance判定為資料中心流量而擋掉（Connection reset / 429），
    這是Yahoo端的限制，不是我們這邊網路設定或程式碼的問題。

    v2改成完全只走證交所（TWSE）與櫃買中心（TPEx）官方的「全市場单日快照」
    端點，這兩個端點都支援指定「過去某一天」查詢，所以我們用迴圈把最近
    N個日曆天都各查一次，自己組出每檔股票近幾天的OHLCV，不再需要
    Yahoo Finance／yfinance。

    這兩個端點的欄位格式已對照多份第三方文件與範例程式碼交叉核對過，
    但畢竟開發環境連不到twse.com.tw/tpex.org.tw，無法端到端實測，
    第一次執行請務必看log，如果欄位對不上，把錯誤或前幾筆原始資料
    貼回來，我再校正。

使用方式：
    1. 放進Cowork工作資料夾（跟v1腳本放同一層即可，v1可以留著或刪掉）
    2. 對Cowork說：「安裝pip install pandas requests --break-system-packages，
       然後執行tw_pattern_screener_v2.py，把結果存到outputs資料夾」
       （注意：v2不需要yfinance了）
    3. 排程／schedule設定跟v1一樣，改指向這支檔案即可

安裝需求：
    pip install pandas requests --break-system-packages
"""

import io
import os
import sys
import time
import json
import subprocess
import tempfile
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 參數設定區
# ------------------------------------------------------------------
VOLUME_MULTIPLIER = 1.5        # 訊號日成交量需為前一日的幾倍以上
LOOKBACK_CALENDAR_DAYS = 12    # 往前抓幾個日曆天（含假日），確保至少湊到4個交易日
MAX_RETRIES = 3                # 每個日期、每個交易所最多重試幾次
RETRY_BACKOFF_SEC = 3          # 重試間隔（秒），每次重試遞增
REQUEST_TIMEOUT = 20           # 單次請求逾時秒數
OUTPUT_DIR = "outputs"

TWSE_MI_INDEX_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
# 2026年8月改版後的正確端點（舊的 stk_quote_download.php 已404）：
# 這支端點支援 date= 參數查詢任意過去交易日的上櫃全市場收盤行情
TPEX_DAILY_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# ------------------------------------------------------------------
# 共用：帶重試的GET請求
# ------------------------------------------------------------------
#
# 這個沙盒環境對 twse.com.tw / tpex.org.tw 的連線不算被封鎖，但透過Python
# requests/urllib3抓大型回應（尤其TPEx單日全市場資料約1.5MB）時，經常在
# chunked transfer過程中斷線（ChunkedEncodingError: Response ended
# prematurely）。實測發現同一個URL改用curl子行程抓，成功率高很多，
# 所以這裡做法是：requests先重試幾次，全部失敗後改用curl當保底方案。

def _fetch_via_curl(url: str, params: dict, timeout: int) -> Optional[str]:
    """用curl子行程抓網頁內容，做為requests失敗後的保底方案。成功回傳文字內容，失敗回傳None。"""
    query = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    full_url = f"{url}?{query}" if query else url
    with tempfile.NamedTemporaryFile(delete=False, suffix=".out") as tf:
        tmp_path = tf.name
    try:
        proc = subprocess.run(
            ["curl", "-s", "-A", HEADERS["User-Agent"], "--max-time", str(timeout),
             "-o", tmp_path, "-w", "%{http_code}", full_url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        http_code = proc.stdout.strip()
        if http_code != "200":
            return None
        with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _get_with_retry(url: str, params: dict, is_json: bool = True):
    """對指定URL發GET請求，失敗時重試；requests全部失敗後改用curl子行程再試一次。全部失敗回傳None（不中斷整支程式）"""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json() if is_json else resp.text
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)

    # requests多次重試都失敗，改用curl保底再試一次（大型回應在這個環境下curl穩定很多）
    text = _fetch_via_curl(url, params, timeout=REQUEST_TIMEOUT + 20)
    if text is not None:
        try:
            return json.loads(text) if is_json else text
        except json.JSONDecodeError as e:
            last_err = e

    logger.warning(f"請求失敗（requests重試{MAX_RETRIES}次＋curl保底皆失敗）：{url} params={params} 最後錯誤：{last_err}")
    return None


def _to_float(s):
    """把帶千分位逗號的字串轉float，無法轉換（例如'--'、空字串）回傳None"""
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "--", "---", "X", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ------------------------------------------------------------------
# 第一部分：抓單一交易日、全市場的OHLCV快照
# ------------------------------------------------------------------

def fetch_twse_day(date: datetime) -> dict:
    """
    抓上市股票某一天的全市場OHLCV快照。
    回傳 {股票代碼: {'open':..,'high':..,'low':..,'close':..,'volume':..}}
    非交易日或抓取失敗回傳空dict。
    """
    date_str = date.strftime("%Y%m%d")
    params = {"response": "json", "date": date_str, "type": "ALLBUT0999"}
    data = _get_with_retry(TWSE_MI_INDEX_URL, params, is_json=True)
    if not data or data.get("stat") != "OK" or "tables" not in data:
        return {}

    # 2026年8月改版後，回傳格式變成 tables[] 陣列（每張表一個title/fields/data），
    # 不再是舊格式的頂層 fields9/data9。逐表比對欄位名稱找出「每日收盤行情」那張表。
    target_table = None
    for t in data["tables"]:
        t_fields = t.get("fields") or []
        if "證券代號" in t_fields and "收盤價" in t_fields:
            target_table = t
            break
    if target_table is None:
        logger.warning(f"TWSE {date_str}：在tables[]裡找不到含「證券代號」「收盤價」欄位的表，"
                        f"實際各表標題：{[t.get('title') for t in data['tables']]}")
        return {}

    fields = target_table["fields"]
    try:
        idx_code = fields.index("證券代號")
        idx_open = fields.index("開盤價")
        idx_high = fields.index("最高價")
        idx_low = fields.index("最低價")
        idx_close = fields.index("收盤價")
        idx_volume = fields.index("成交股數")
    except ValueError:
        logger.warning(f"TWSE {date_str}：欄位名稱跟預期不符，實際欄位：{fields}")
        return {}

    result = {}
    for row in target_table["data"]:
        code = str(row[idx_code]).strip()
        if not code.isdigit() or len(code) != 4:
            continue  # 排除權證、ETF等非4碼普通股代碼
        o, h, l, c, v = (
            _to_float(row[idx_open]), _to_float(row[idx_high]),
            _to_float(row[idx_low]), _to_float(row[idx_close]),
            _to_float(row[idx_volume]),
        )
        if None in (o, h, l, c, v):
            continue  # 當日無成交或資料不完整
        result[f"{code}.TW"] = {"open": o, "high": h, "low": l, "close": c, "volume": v}
    return result


def fetch_tpex_day(date: datetime) -> dict:
    """
    抓上櫃股票某一天的全市場OHLCV快照。
    回傳 {股票代碼: {'open':..,'high':..,'low':..,'close':..,'volume':..}}
    非交易日或抓取失敗回傳空dict。
    """
    # 新版網站的AJAX端點，date用西元年格式（YYYY/MM/DD），response=json
    date_str = date.strftime("%Y/%m/%d")
    params = {"date": date_str, "response": "json"}
    data = _get_with_retry(TPEX_DAILY_URL, params, is_json=True)
    if not data or "tables" not in data or not data["tables"]:
        return {}

    table = data["tables"][0]
    fields = table.get("fields") or []
    try:
        idx_code = fields.index("代號")
        idx_open = fields.index("開盤")
        idx_high = fields.index("最高")
        idx_low = fields.index("最低")
        idx_close = fields.index("收盤")
        idx_volume = fields.index("成交股數")
    except ValueError:
        logger.warning(f"TPEx {date_str}：欄位名稱跟預期不符，實際欄位：{fields}")
        return {}

    result = {}
    for row in table.get("data", []):
        try:
            code = str(row[idx_code]).strip()
        except Exception:
            continue
        if not code.isdigit() or len(code) != 4:
            continue
        o, h, l, c, v = (
            _to_float(row[idx_open]), _to_float(row[idx_high]), _to_float(row[idx_low]),
            _to_float(row[idx_close]), _to_float(row[idx_volume]),
        )
        if None in (o, h, l, c, v):
            continue
        result[f"{code}.TWO"] = {"open": o, "high": h, "low": l, "close": c, "volume": v}
    return result


# ------------------------------------------------------------------
# 第二部分：累積近N天資料，組成逐檔的OHLCV時間序列
# ------------------------------------------------------------------

def build_history() -> dict:
    """
    往前掃LOOKBACK_CALENDAR_DAYS天，逐日抓TWSE+TPEx全市場快照，
    回傳 {股票代碼: [依日期由舊到新排序的{'date','open','high','low','close','volume'}, ...]}
    """
    history: dict = {}
    today = datetime.now()
    dates = [today - timedelta(days=i) for i in range(LOOKBACK_CALENDAR_DAYS, -1, -1)]  # 舊到新

    for d in dates:
        if d.weekday() >= 5:  # 週六日直接跳過，不必浪費請求
            continue
        date_str = d.strftime("%Y-%m-%d")
        logger.info(f"抓取 {date_str} 的全市場快照...")

        twse_day = fetch_twse_day(d)
        tpex_day = fetch_tpex_day(d)
        day_data = {**twse_day, **tpex_day}

        if not day_data:
            logger.info(f"{date_str}：無資料（可能是假日或抓取失敗），跳過")
            continue

        logger.info(f"{date_str}：取得 {len(day_data)} 檔股票資料")
        for ticker, ohlcv in day_data.items():
            history.setdefault(ticker, []).append({"date": date_str, **ohlcv})

        time.sleep(1)  # 每天之間稍微間隔，降低被限流機率

    return history


# ------------------------------------------------------------------
# 第三部分：型態判斷邏輯（與v1相同，已測試過，未變更）
# ------------------------------------------------------------------

def check_pattern(df: pd.DataFrame, volume_multiplier: float = VOLUME_MULTIPLIER) -> Optional[dict]:
    """
    檢查最近OHLCV資料是否符合「連跌三日＋大紅K吃前高＋爆量」型態。
    df 需由舊到新排序，且包含 Open/High/Low/Close/Volume 欄位。
    """
    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(df) < 4:
        return None

    last4 = df.iloc[-4:]
    three_days = last4.iloc[:3]
    signal = last4.iloc[-1]

    closes = three_days["Close"].values
    three_days_falling = all(closes[i] < closes[i - 1] for i in range(1, len(closes)))

    is_red = signal["Close"] > signal["Open"]
    breaks_high = signal["Close"] > three_days["High"].max()

    prev_volume = three_days["Volume"].iloc[-1]
    volume_spike = prev_volume > 0 and signal["Volume"] > prev_volume * volume_multiplier

    if three_days_falling and is_red and breaks_high and volume_spike:
        signal_date = signal.name.strftime("%Y-%m-%d") if hasattr(signal.name, "strftime") else str(signal.name)
        return {
            "訊號日期": signal_date,
            "開盤": round(float(signal["Open"]), 2),
            "收盤": round(float(signal["Close"]), 2),
            "前三日高點": round(float(three_days["High"].max()), 2),
            "當日量": int(signal["Volume"]),
            "前一日量": int(prev_volume),
            "量能倍數": round(float(signal["Volume"] / prev_volume), 2) if prev_volume else None,
        }
    return None


def screen_history(history: dict) -> pd.DataFrame:
    """對累積好的歷史資料逐檔跑型態判斷，回傳符合條件的整理表"""
    matches = []
    for ticker, records in history.items():
        if len(records) < 4:
            continue
        df = pd.DataFrame(records).set_index("date")
        df.index = pd.to_datetime(df.index)
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                 "close": "Close", "volume": "Volume"}).sort_index()
        result = check_pattern(df)
        if result:
            result["股票代碼"] = ticker
            matches.append(result)
            logger.info(f"符合條件：{ticker} | 收盤{result['收盤']} 量能{result['量能倍數']}倍")

    result_df = pd.DataFrame(matches)
    if not result_df.empty:
        result_df = result_df[
            ["股票代碼", "訊號日期", "開盤", "收盤", "前三日高點", "當日量", "前一日量", "量能倍數"]
        ].sort_values("量能倍數", ascending=False).reset_index(drop=True)
    return result_df


# ------------------------------------------------------------------
# 主程式
# ------------------------------------------------------------------

def main():
    today_str = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"開始執行台股型態篩選 v2（不依賴Yahoo Finance）（{today_str}）")

    history = build_history()
    total_tickers = len(history)
    logger.info(f"共取得 {total_tickers} 檔股票的歷史資料，開始逐檔判斷型態...")

    result_df = screen_history(history)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, f"pattern-screen-{today_str}.csv")

    if total_tickers == 0:
        logger.warning("完全沒有抓到任何股票資料，不寫出報表，請檢查log找出是哪個環節失敗。")
        return

    result_df.to_csv(output_path, index=False, encoding="utf-8-sig")
    if result_df.empty:
        logger.info(f"已成功掃描 {total_tickers} 檔股票，今日沒有符合條件的標的。報表（空表）存至 {output_path}")
    else:
        logger.info(f"共找到 {len(result_df)} 檔符合條件的股票，報表存至 {output_path}")


def _selftest():
    """快速測試（不連網）：確認check_pattern判斷邏輯正確。用法：python tw_pattern_screener_v2.py --selftest"""
    dates = pd.date_range("2026-08-10", periods=4, freq="B")
    mock = pd.DataFrame({
        "Open":   [100, 96, 92, 95],
        "High":   [101, 97, 93, 108],
        "Low":    [95, 91, 88, 94],
        "Close":  [97, 93, 89, 107],
        "Volume": [5000, 4800, 4500, 9000],
    }, index=dates)
    result = check_pattern(mock)
    print("自我測試結果（應為符合條件的dict，而非None）：")
    print(result)
    assert result is not None, "自我測試失敗：預期符合條件"
    print("自我測試通過。")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    else:
        main()
