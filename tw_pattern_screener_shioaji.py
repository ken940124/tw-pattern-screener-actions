"""
台股「連跌三日＋大紅K吃前高＋爆量」型態策略 —— 本地紙上模擬倉版（純Python，不連永豐Shioaji）
================================================================================
把型態篩選（tw_pattern_screener_v2.py）＋回測結果（backtest_calibrate.py）串起來，
讓程式每個交易日自動：

  1. 檢查手上模擬倉位：有沒有觸發停損／停利／持有超過最長天數，有的話記錄出場、結清部位
  2. 掃描今天的全市場，找出符合型態的新訊號
  3. 只對「通過回測濾網」的訊號記錄進場（見下方「進場濾網」段落，數字都來自
     backtest_calibrate.py／calibration_grid_results.json的實測結果，不是憑感覺設的）
  4. 把每筆進出場都記錄在本地的 positions.json / trade_log.csv，方便之後檢討績效

=========================== 這版改了什麼、為什麼 ===========================

這支程式原本是接永豐金證券Shioaji模擬環境真的下單，但實際使用下來發現兩個問題：

  1. Shioaji的Solace binary協定連線在雲端沙盒會被搞壞，只能在自己電腦跑；而自己電腦這邊
     Windows Task Scheduler排程又一直卡關（不是登入被中斷，就是連線逾時），導致自動化長期
     不穩定。
  2. 更根本的是：就算連線正常，永豐模擬環境的真實成交結果沒有對應的網頁介面可以查——只能
     用api.list_positions()這種API呼叫才看得到，日常根本不會去查，形同看不到。而先前唯一
     一次reconcile_positions_with_broker()真的執行成功時，發現永豐那邊的「真實持倉」跟本地
     記錄的完全兜不起來（本地手動維護的紀錄從未真的在永豐那邊下單成交過）。

既然真實broker端的持倉平常看不到、也不重要，乾脆拿掉Shioaji這一整層，改成純本地的紙上
模擬：不登入、不下單、不查詢真實持倉，只用公開市場資料＋本地positions.json自己算進出場、
算損益。這樣少了「登入永豐」這個環節，前面卡住自動化的那些連線/認證問題也一併消失，
Task Scheduler只需要負責跑一支不用連外部券商的Python腳本，穩定性會高很多。

=========================== 使用前必看 ===========================

1. 這支程式現在完全不需要永豐帳號、API金鑰、或Shioaji套件，只需要一般的Python環境
   （pandas + requests）就能跑，可以放在雲端或本機任何地方執行。
2. 進場濾網、停損停利數字都只是根據回測結果給的起點，不是保證有效，實際使用前請自行
   檢視、必要時調整，這不構成投資建議。
3. 這整支程式從頭到尾都是「本地紙上模擬」，不會、也沒有能力送出任何真實或模擬的下單
   委託，純粹是記帳用途。

安裝需求：
    pip install pandas requests --break-system-packages
"""
import os
import json
import logging
from datetime import datetime, timedelta

import pandas as pd

# 沿用v2篩選腳本的資料抓取邏輯（TWSE/TPEx官方全市場快照）
from tw_pattern_screener_v2 import (
    fetch_twse_day, fetch_tpex_day, VOLUME_MULTIPLIER,
    _get_with_retry, TWSE_MI_INDEX_URL,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 參數設定區
# ------------------------------------------------------------------
LOOKBACK_CALENDAR_DAYS = 45      # 抓近45個日曆天，確保個股MA20跟大盤MA20都算得出來
MA_WINDOW = 20                   # 回測驗證過的均線濾網天數

STOP_LOSS_PCT = -0.10            # 停損：虧損超過10%出場
TAKE_PROFIT_PCT = 0.18           # 停利：獲利超過18%出場
MAX_HOLD_DAYS = 10               # 最長持有天數（交易日），超過就強制出場
# 以上三個數字是用近3年(2023-08~2026-08)歷史資料、實際跑過一次網格回測校準過的結果（見
# backtest_calibrate.py／calibration_grid_results.json），不是憑經驗亂猜的起點。
# 但校準結果有兩個重要但書要注意：
#   1) 回測找到的「最佳組合」落在網格邊界（停損停利越寬、持有天數越短，結果越好，一路到網格邊緣
#      都還在改善），代表最佳解可能還在網格外面，這裡選的是邊界附近相對溫和的一組，不是网格裡分數
#      最高的那組(-12%/+20%/10天)，用意是降低過度配適(overfitting)的風險，但仍要小心這組參數
#      本身也可能只是這3年特定市況下的產物，換一段時間不一定成立。
#   2) 更關鍵的發現：這3年剛好是台股罕見的大多頭（加權指數同期漲了約180%，年化近48%），拿同樣
#      這3年做無條件比較，「什麼都不做、單純buy&hold大盤10個交易日」的平均報酬率(約1.56%)反而
#      明顯贏過這套型態策略套用最佳化參數後的平均報酬率(約0.33%~0.4%)。也就是說目前沒有證據顯示
#      這套型態選股邏輯本身有超額報酬(alpha)，比較像是在一個強力多頭市場裡，透過「個股+大盤都站上
#      MA20」這個濾網間接搭到了大盤上漲的順風車，扣掉交易成本(手續費+證交稅，來回粗估0.4%~0.5%)
#      之後，就算是校準後的最佳參數，淨期望值很可能打平甚至轉負。這點務必要知道，不是只看報酬率
#      數字是正的就代表這套策略真的有效。

RISK_PER_TRADE_NTD = 10000       # 每一筆交易觸發停損時，帳面上願意承受的最大虧損金額（新台幣）——這是
                                  # 「風險等權重」部位大小的核心參數：不同股票的曝險金額不再固定一樣，
                                  # 而是波動度(用停損距離近似)不一樣的股票，會分配到不同股數，讓每一筆
                                  # 交易「虧到停損時」虧的金額都差不多。
                                  # 股數 = 無條件捨去(風險金額 / (參考價 * 停損百分比的絕對值))。
                                  # 要注意：因為目前停損是固定百分比（不是隨個股波動度調整的ATR停損），
                                  # 這個公式其實等於「無條件捨去(風險金額/停損% / 參考價)」，也就是
                                  # 「固定金額等權重」乘上一個常數(風險金額/停損%)而已，本質上跟之前的
                                  # PER_STOCK_CAPITAL_CAP等權重寫法是同一件事，只是換了個角度定義金額
                                  # （這裡剛好算出來接近10萬元，是因為10000/10%=100000）。如果之後想要
                                  # 讓風險等權重真正跟「固定金額等權重」產生差異，需要把停損改成ATR之類
                                  # 隨個股波動度調整的版本，不然這兩種寫法在數學上是等價的。
MAX_NEW_POSITIONS_PER_DAY = 6    # 每天最多開幾檔新倉，訊號太多時只取「量能倍數」最高的前幾名，
                                  # 避免像8/26那樣一次冒出20檔、單日資金需求爆表
MAX_TOTAL_POSITIONS = 20         # 同時最多持有幾檔部位，超過就不再開新倉（就算當天有訊號也跳過），
                                  # 等舊部位出場、騰出名額之後才會再進場

MIN_SIGNAL_VOLUME_LOTS = 200     # 訊號日（D4，型態判斷用的最後一天）成交量至少要有這麼多張，
                                  # 太冷門、量太小的股票不記錄進場（避免現實中根本滑價滑很大）
MIN_SIGNAL_VOLUME_SHARES = MIN_SIGNAL_VOLUME_LOTS * 1000

POSITIONS_FILE = "positions.json"
TRADE_LOG_FILE = "outputs/trade_log.csv"
HISTORY_CACHE_FILE = "history_cache.json"


# ------------------------------------------------------------------
# 第一部分：抓資料＋算濾網用的均線（跟v2/回測腳本邏輯一致）
# ------------------------------------------------------------------

def _fetch_taiex_close(date):
    """抓當天大盤加權指數收盤值，跟fetch_twse_day同一個端點，這裡獨立解析一次表0（指數表）"""
    params = {"response": "json", "date": date.strftime("%Y%m%d"), "type": "ALLBUT0999"}
    raw = _get_with_retry(TWSE_MI_INDEX_URL, params, is_json=True)
    if not raw or "tables" not in raw:
        return None
    for t in raw["tables"]:
        fs = t.get("fields") or []
        if "指數" in fs and "收盤指數" in fs:
            i_name, i_close = fs.index("指數"), fs.index("收盤指數")
            for row in t.get("data", []):
                if row[i_name] == "發行量加權股價指數":
                    try:
                        return float(str(row[i_close]).replace(",", ""))
                    except (ValueError, TypeError):
                        return None
    return None


def _load_history_cache():
    if os.path.exists(HISTORY_CACHE_FILE):
        with open(HISTORY_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_history_cache(cache):
    with open(HISTORY_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def build_recent_history():
    """抓近LOOKBACK_CALENDAR_DAYS天的TWSE+TPEx全市場快照，同時把大盤加權指數一併記下來。

    這版加了本地快取（history_cache.json）：每天真正需要對TWSE/TPEx發網路請求的，
    正常情況下只有「快取裡還沒有的日期」——也就是今天這一天（2次請求＋1次大盤指數），
    而不是每天都把過去45天全部重抓一次。

    這個改動是因為排到雲端排程之後才發現的問題：雲端環境對外連線到twse.com.tw／
    tpex.org.tw這種外部網站，明顯比在自己電腦上執行時慢很多／不穩定很多，每一次
    請求失敗都要重試多次＋curl保底，45天×2個交易所全部都要重抓的話，遇到連線不順
    時單次執行可能拖到超過一小時，實務上完全不能用。改成只抓「快取沒有的日期」之後，
    正常情況下每天只需要2~3次網路請求，就算連線不順、重試很多次，也不會拖太久，
    大幅降低了雲端排程被網路品質拖垮的風險。

    快取只保留近LOOKBACK_CALENDAR_DAYS天，超出這個視窗的舊日期會被清掉，避免快取檔案
    無限長大；呼叫端（tw_pattern_screener_shioaji.py跑在雲端排程時）另外會把這個快取
    同步進Artifact的資料庫，讓快取本身也能跨「每次都是全新容器」保存下來，不然這個
    快取機制在雲端排程情境下每次都會是空的，等於沒用。

    回傳 ({ticker: DataFrame(含ma20)}, taiex_df(含ma20))"""
    cache = _load_history_cache()

    today = datetime.now()
    dates = [today - timedelta(days=i) for i in range(LOOKBACK_CALENDAR_DAYS, -1, -1)]
    dates = [d for d in dates if d.weekday() < 5]
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]

    missing = [d for d, ds in zip(dates, date_strs) if ds not in cache]

    # 2026-09-17修正：原本的邏輯是「這天只要在cache裡有key，就當作完全抓齊了，不會再發任何
    # 請求」。但實務上發現，只要TWSE成功、TPEx那天剛好失敗（例如雲端排程的IP被tpex.org.tw
    # 擋掉／連線不穩逾時），merged還是非空（有TWSE部分），這天照樣會被寫進cache——結果就是
    # 「上櫃股票那天的資料永遠遺失，而且往後每天都不會再重試」，因為判斷「要不要重抓」只看
    # 這天在不在cache裡，不看cache裡的資料是不是完整。這裡改成額外追蹤twse_ok/tpex_ok兩個
    # flag，只要某天的tpex_ok不是True，之後每次執行都會單獨補抓那天的TPEx部分（不會浪費
    # 請求重抓已經成功的TWSE部分），直到補齊為止。對於這次修正上線之前就已經存在、格式裡沒有
    # tpex_ok欄位的舊快取資料，用「這天merged裡有沒有任何.TWO股票」回推：完全沒有.TWO代表
    # 當初這天TPEx大概率是失敗的，一樣會被排進補抓名單（正常交易日一定會有上百檔上櫃股票，
    # 不可能真的是0檔），這樣舊資料也能被這次的修正自動修復，不用手動改cache檔案。
    needs_tpex_retry = []
    for d, ds in zip(dates, date_strs):
        if ds not in cache:
            continue  # 這天完全沒抓過，已經在上面的missing裡了，等一下會整天重抓
        entry = cache[ds]
        tpex_ok = entry.get("tpex_ok")
        if tpex_ok is None:
            tpex_ok = any(k.endswith(".TWO") for k in entry.get("merged", {}))
        if not tpex_ok:
            needs_tpex_retry.append((d, ds))

    if missing:
        logger.info(f"歷史快取缺 {len(missing)} 天的資料，需要向TWSE/TPEx發請求補齊："
                    f"{[d.strftime('%Y-%m-%d') for d in missing]}")
    if needs_tpex_retry:
        logger.info(f"歷史快取有 {len(needs_tpex_retry)} 天先前TPEx（上櫃）抓取失敗，"
                    f"這次會單獨重新補抓TPEx部分：{[ds for _, ds in needs_tpex_retry]}")
    if not missing and not needs_tpex_retry:
        logger.info("歷史快取已涵蓋所需的所有日期，這次不需要對TWSE/TPEx發任何請求")

    for d in missing:
        ds = d.strftime("%Y-%m-%d")
        twse = fetch_twse_day(d)
        tpex = fetch_tpex_day(d)
        merged = {**twse, **tpex}
        taiex_close = _fetch_taiex_close(d) if twse else None
        if merged or taiex_close is not None:
            cache[ds] = {
                "merged": merged, "taiex": taiex_close,
                "twse_ok": bool(twse), "tpex_ok": bool(tpex),
            }
        # 抓不到資料的日期（假日、還沒收盤、或抓取失敗）故意不寫進快取，
        # 這樣下次執行還會再嘗試，不會把「抓失敗」誤存成「這天沒交易」

    for d, ds in needs_tpex_retry:
        tpex = fetch_tpex_day(d)
        if tpex:
            entry = cache[ds]
            entry["merged"] = {**entry.get("merged", {}), **tpex}
            entry["tpex_ok"] = True
            logger.info(f"{ds}：補抓TPEx成功，補進 {len(tpex)} 檔上櫃股票收盤價")
        else:
            cache[ds]["tpex_ok"] = False
            logger.warning(f"{ds}：這次補抓TPEx還是失敗，下次執行會繼續重試")

    # 清掉超出目前lookback視窗的舊日期，避免快取無限長大
    keep_set = set(date_strs)
    for ds in list(cache.keys()):
        if ds not in keep_set:
            del cache[ds]
    _save_history_cache(cache)

    per_ticker = {}
    taiex_records = []
    for ds in date_strs:
        entry = cache.get(ds)
        if not entry:
            continue
        for ticker, ohlcv in entry.get("merged", {}).items():
            per_ticker.setdefault(ticker, []).append({"date": ds, **ohlcv})
        if entry.get("taiex") is not None:
            taiex_records.append({"date": ds, "close": entry["taiex"]})

    frames = {}
    for ticker, records in per_ticker.items():
        df = pd.DataFrame(records).drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
        df["ma20"] = df["close"].rolling(MA_WINDOW).mean()
        frames[ticker] = df

    taiex_df = pd.DataFrame(taiex_records).drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    if not taiex_df.empty:
        taiex_df["ma20"] = taiex_df["close"].rolling(MA_WINDOW).mean()
    return frames, taiex_df


def check_pattern(df):
    """連跌三日＋紅K吃前高＋爆量的型態判斷。

    修正說明（重要，之前這裡有個會讓進場價不可能真的成交到的bug）：
    舊版把「訊號日」設成df最後一天（也就是今天），然後直接拿當天的收盤價當進場價
    （entry_price = 訊號日收盤價、entry_date = 今天）。但當天收盤價要等收盤那一刻才知道，
    等你知道的時候已經不可能用那個價格成交了——這在回測裡沒差，但套到即時的每日自動化上，
    等於假設了一個做不到的進場價，會讓帳上的進場成本系統性地偏樂觀。

    這裡改成：訊號用df「倒數第2天」判斷（也就是已經確定收盤、資料完整的那一天），
    進場價用df「最後一天」（即訊號隔天，也是排程實際執行的當天）的**開盤價**——
    這是收盤後才跑的每日排程當下唯一「已經發生、已知、理論上真的能成交」的價格。
    這個「訊號日隔天用開盤價進場」的作法，其實才是原本portfolio裡大多數舊部位
    （8/21~8/26進場那批）真正使用的方式，回傳欄位也一併補上「進場日期」「進場開盤價」。
    """
    d = df.dropna(subset=["open", "high", "low", "close", "volume"])
    if len(d) < 5:  # 三天下跌 + 訊號日 + 進場日，共5天資料才夠判斷
        return None
    last5 = d.iloc[-5:]
    three_days, signal, entry_day = last5.iloc[:3], last5.iloc[3], last5.iloc[4]

    closes = three_days["close"].values
    falling = all(closes[i] < closes[i - 1] for i in range(1, len(closes)))
    is_red = signal["close"] > signal["open"]
    breaks_high = signal["close"] > three_days["high"].max()
    prev_vol = three_days["volume"].iloc[-1]
    vol_ratio = signal["volume"] / prev_vol if prev_vol else None
    vol_spike = prev_vol > 0 and vol_ratio is not None and vol_ratio >= VOLUME_MULTIPLIER

    if falling and is_red and breaks_high and vol_spike:
        return {"訊號日期": signal["date"], "收盤": float(signal["close"]),
                "訊號日成交量": float(signal["volume"]),
                "量能倍數": round(float(vol_ratio), 2), "own_ma20": signal.get("ma20"),
                "進場日期": entry_day["date"], "進場開盤價": float(entry_day["open"])}
    return None


def scan_today_signals(frames, taiex_ma20_pass):
    """掃今天的訊號，套用回測驗證過的濾網：個股站上MA20 且 大盤站上MA20，
    再加上流動性濾網：訊號日（D4）成交量至少要有MIN_SIGNAL_VOLUME_LOTS張，太冷門的不買。"""
    signals = []
    for ticker, df in frames.items():
        sig = check_pattern(df)
        if not sig:
            continue
        own_ma20_pass = pd.notna(sig["own_ma20"]) and sig["收盤"] > sig["own_ma20"]
        volume_pass = sig["訊號日成交量"] >= MIN_SIGNAL_VOLUME_SHARES
        # 回測結果：個股+大盤都站上MA20是測試過表現相對最穩的組合，其他組合沒有明顯優勢
        # （爆量倍數拉高到3倍以上反而變差，這裡刻意不加這個濾網，見backtest-report.md）
        if own_ma20_pass and taiex_ma20_pass and volume_pass:
            sig["股票代碼"] = ticker
            signals.append(sig)
        elif own_ma20_pass and taiex_ma20_pass and not volume_pass:
            logger.info(f"{ticker} 型態＋均線濾網都符合，但訊號日成交量只有"
                        f"{sig['訊號日成交量']/1000:.0f}張，低於{MIN_SIGNAL_VOLUME_LOTS}張門檻，跳過")
    return signals


def taiex_ma20_status(taiex_df):
    """判斷「訊號日」（也就是df倒數第2天，跟check_pattern的訊號日定義一致，
    不是最新一天——最新一天是進場日，用開盤價進場，當天大盤還沒收盤，看不到當天的濾網結果）
    的大盤是否站上自身20日均線（用build_recent_history一併抓好的taiex_df）"""
    if taiex_df.empty or len(taiex_df) < MA_WINDOW + 2 or pd.isna(taiex_df.iloc[-2]["ma20"]):
        logger.warning("大盤資料不足以算MA20，本次視為不通過大盤濾網（保守處理）")
        return False
    signal_day = taiex_df.iloc[-2]
    return bool(signal_day["close"] > signal_day["ma20"])


# ------------------------------------------------------------------
# 第二部分：部位管理（本地JSON檔記錄，純紙上模擬，不對外下任何真實或模擬單）
# ------------------------------------------------------------------

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_positions(positions):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


def log_trade(row):
    os.makedirs("outputs", exist_ok=True)
    df_row = pd.DataFrame([row])
    if os.path.exists(TRADE_LOG_FILE):
        df_row.to_csv(TRADE_LOG_FILE, mode="a", header=False, index=False, encoding="utf-8-sig")
    else:
        df_row.to_csv(TRADE_LOG_FILE, mode="w", header=True, index=False, encoding="utf-8-sig")


def plan_order_size(reference_price, risk_per_trade=RISK_PER_TRADE_NTD, stop_loss_pct=STOP_LOSS_PCT):
    """依「風險等權重」決定要記錄幾股：每一筆交易觸發停損時，帳面上虧的金額都控制在
    risk_per_trade附近。純本地模擬不需要再依真實券商的「整股／零股」下單規則分單，
    直接算出總股數即可。

    股數 = 無條件捨去(風險金額 / (參考價格 * |停損百分比|))
    """
    per_share_risk = reference_price * abs(stop_loss_pct)
    if per_share_risk <= 0:
        return 0
    return int(risk_per_trade // per_share_risk)


def count_trading_days(start_date_str, end_date):
    """算進場日（不含）到現在（含）之間經過了幾個交易日，用「非週末」近似交易日
    （這支程式其他地方，例如build_recent_history，也是用同樣「排除週六日」的簡化方式，
    沒有特別排除國定假日，剛好卡到連假的話，這裡算出來的天數會比實際交易日略多一點，
    偏保守——也就是可能會比回測定義的20個交易日稍微晚一點點才出場，不會太早出場）。"""
    start = datetime.strptime(start_date_str, "%Y-%m-%d")
    days = 0
    d = start
    while d < end_date:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def main():
    today_str = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"=== 型態策略本地模擬倉每日流程開始（{today_str}）===")

    positions = load_positions()

    frames, taiex_df = build_recent_history()
    taiex_ma20_pass = taiex_ma20_status(taiex_df)
    logger.info(f"大盤是否站上MA20：{taiex_ma20_pass}")

    # --- 1. 先檢查現有部位，觸發停損/停利/超過最長持有天數就出場（純記帳，不送出任何委託）---
    for ticker, pos in list(positions.items()):
        df = frames.get(ticker)
        if df is None or df.empty:
            continue
        latest = df.iloc[-1]
        cur_price = float(latest["close"])
        ret = (cur_price - pos["entry_price"]) / pos["entry_price"]
        hold_days = count_trading_days(pos["entry_date"], datetime.now())

        exit_reason = None
        if ret <= STOP_LOSS_PCT:
            exit_reason = "停損"
        elif ret >= TAKE_PROFIT_PCT:
            exit_reason = "停利"
        elif hold_days >= MAX_HOLD_DAYS:
            exit_reason = "到期出場"

        if exit_reason:
            logger.info(f"{ticker} 出場（{exit_reason}），報酬{ret*100:.2f}%")
            log_trade({"股票代碼": ticker, "進場日期": pos["entry_date"], "進場價": pos["entry_price"],
                       "出場日期": today_str, "出場價": cur_price, "出場原因": exit_reason,
                       "報酬率": round(ret, 4), "股數": pos["quantity_shares"]})
            del positions[ticker]

    save_positions(positions)

    # --- 2. 掃今天的新訊號，套用回測濾網＋流動性濾網＋每日/總量上限後，依風險等權重算股數、記錄進場 ---
    signals = scan_today_signals(frames, taiex_ma20_pass)
    logger.info(f"今天符合型態＋均線＋成交量濾網的訊號共 {len(signals)} 檔")

    # 部位總量上限：已經滿了就完全不開新倉；沒滿的話，訊號依「量能倍數」由高到低排序，
    # 只取排名前MAX_NEW_POSITIONS_PER_DAY名（且不超過剩餘名額），避免單日訊號暴增時資金需求跟著暴增
    slots_left = MAX_TOTAL_POSITIONS - len(positions)
    if slots_left <= 0:
        logger.warning(f"目前已持有{len(positions)}檔，達到總量上限{MAX_TOTAL_POSITIONS}檔，今天不開新倉")
        signals = []
    else:
        candidates = [s for s in signals if s["股票代碼"] not in positions]
        candidates.sort(key=lambda s: -s["量能倍數"])
        take_n = min(MAX_NEW_POSITIONS_PER_DAY, slots_left)
        skipped = candidates[take_n:]
        signals = candidates[:take_n]
        if skipped:
            logger.info(f"訊號數超過每日上限{MAX_NEW_POSITIONS_PER_DAY}檔或剩餘名額{slots_left}檔，"
                        f"以下{len(skipped)}檔量能倍數較低、本次不進場：{[s['股票代碼'] for s in skipped]}")

    for sig in signals:
        ticker = sig["股票代碼"]
        if ticker in positions:
            logger.info(f"{ticker} 已經有部位，跳過重複進場")
            continue
        entry_price = sig["進場開盤價"]  # 訊號隔天（也就是進場日、排程實際執行當天）的開盤價，是真的能成交到的價格
        entry_date = sig["進場日期"]
        total_shares = plan_order_size(entry_price)
        if total_shares < 1:
            logger.warning(f"{ticker} 參考價{entry_price}，風險金額{RISK_PER_TRADE_NTD:,.0f}元連1股都買不到，跳過這檔")
            continue
        positions[ticker] = {
            "entry_date": entry_date, "entry_price": entry_price,
            "quantity_shares": total_shares, "量能倍數": sig["量能倍數"],
        }
        logger.info(f"{ticker} 記錄進場，{total_shares}股，參考價{entry_price}（約{total_shares*entry_price:,.0f}元），"
                    f"量能{sig['量能倍數']}倍")

    save_positions(positions)
    logger.info(f"=== 流程結束，目前持有 {len(positions)} 檔部位 ===")

    # --- 3. 用剛剛已經抓好的frames順手更新儀表板，不用再多打一次API ---
    try:
        import generate_dashboard as dash
        latest_prices = {t: float(df.iloc[-1]["close"]) for t, df in frames.items() if not df.empty}
        trade_log_df = dash.load_trade_log()
        all_tickers = set(positions.keys()) | (
            set(trade_log_df["股票代碼"].tolist()) if not trade_log_df.empty else set()
        )
        stock_names = dash.fetch_stock_names(list(all_tickers))
        holdings_rows = dash.build_holdings_rows(positions, latest_prices, stock_names)
        html = dash.render_html(holdings_rows, trade_log_df, stock_names)
        os.makedirs("outputs", exist_ok=True)
        with open(dash.DASHBOARD_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"儀表板已更新：{dash.DASHBOARD_FILE}，用瀏覽器打開它就能看目前庫存跟歷史交易。")
    except Exception as e:
        logger.warning(f"儀表板更新失敗（不影響記帳本身）：{e}。可以另外手動執行 python generate_dashboard.py 補產生。")


if __name__ == "__main__":
    main()
