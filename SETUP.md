# 設定步驟（一次性）

這個資料夾已經包含目前雲端Artifact db裡最新的真實狀態（20檔持倉、30筆歷史交易、33天的
TWSE/TPEx歷史快取），接手之後會從這裡繼續往下跑，不會歸零重來。

## 1. 建立GitHub repo

1. 到GitHub網站建立一個新的repo（建議設成private），例如命名為`tw-pattern-screener`。
2. 建立時**不要**勾選「Add a README」，保持全新空repo。

## 2. 把這個資料夾的內容推上去

在你自己電腦上（例如用VS Code的終端機），切到這個資料夾之後執行：

```bash
git init
git add .
git commit -m "初始化：從雲端Artifact db匯入目前狀態"
git branch -M main
git remote add origin https://github.com/<你的帳號>/<repo名稱>.git
git push -u origin main
```

如果`git push`要求登入，用你平常登入GitHub的方式（瀏覽器跳轉或個人存取權杖）完成即可，
這一步是你自己跟GitHub之間的認證，不需要交給任何人。

## 3. 打開workflow的寫入權限

進repo的 **Settings → Actions → General**，捲到最下面「Workflow permissions」，
選 **Read and write permissions**，儲存。（沒開這個的話，workflow跑完會因為沒有權限
`git push`回repo而失敗。）

## 4. 打開GitHub Pages

進repo的 **Settings → Pages**，「Build and deployment」的Source選
**Deploy from a branch**，Branch選 **main**，資料夾選 **/docs**，按Save。

第一次執行完成後（見下一步），這裡會出現一個網址，格式大概是：
`https://<你的帳號>.github.io/<repo名稱>/`，這就是之後每天看儀表板的固定網址。

## 5. 手動觸發一次，確認整條流程沒問題

進repo的 **Actions** 分頁，左側點選「台股型態策略每日自動更新」，右上角會有
**Run workflow** 按鈕，點下去手動跑一次（不用等到明天15:30台北時間）。

跑完後檢查：
- 這次執行是綠勾勾（成功）還是紅叉（失敗）——點進去可以看詳細log，
  特別留意有沒有出現連線twse.com.tw／tpex.org.tw失敗的訊息（正常預期是不會有）。
- repo裡`positions.json`、`outputs/trade_log.csv`、`history_cache.json`、
  `docs/index.html`這幾個檔案有沒有出現新的commit。
- 第4步設定的Pages網址打開後，儀表板內容是不是最新的。

確認以上都正常之後，之後每個交易日15:30台北時間就會自動執行，不需要再手動按。

## 之後如果要調整策略參數

直接編輯`tw_pattern_screener_shioaji.py`最上面「參數設定區」那幾個常數
（停損/停利/最長持有天數/風險金額/單日與總量上限等），commit＋push上去就會生效，
下次排程執行時會套用新參數（但已經開的舊倉位不會回溯套用新規則，只影響之後的
出場判斷跟新進場）。

## 待確認：舊的雲端自動化

目前Claude這邊的雲端排程（每日15:00觸發的「台股型態策略每日自動更新」）**還沒關掉**，
等這裡的GitHub Actions版本確認連續穩定跑了幾天、儀表板數字也對得起來之後，
再麻煩回來跟Claude說一聲，把舊的雲端排程停用，避免兩邊同時在跑、又出現分岔的狀態。
