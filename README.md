# Morning English：每天自動產生的 5 分鐘英文 Podcast

每天早上台灣時間約 5:30，雲端自動：抓天氣和兒童新聞 → Claude 寫腳本 → Google TTS 念成音檔 → 更新私人 Podcast。
手機（Apple Podcasts）自動下載，上車連藍牙或 CarPlay 就能播。

**每集內容**：問候＋今日天氣 → 前一天兒童新聞（BBC Newsround，用簡單英文重述）→ 主題故事（動物、古典音樂、太空、火車、美食、繪畫、發明、運動、旅行，9 天一輪）→ 4 個 PET 程度單字複習（每個念完留 4 秒給孩子跟讀）→ 結尾。

---

## 一、事前準備（約 30 分鐘）

### 1. Anthropic API key（寫腳本用）
到 console.anthropic.com 建立帳號、儲值少額費用，建立 API key。這和 Claude Pro 訂閱是分開計費的。你做 AICoach 用的 key 也可以共用。

### 2. Google Cloud TTS key（念音檔用）
1. 到 console.cloud.google.com 建立專案，並**啟用帳單**（要綁信用卡；以每月約 12 萬字元的用量，在官方免費額度內）。
2. 搜尋並啟用 **Cloud Text-to-Speech API**。
3. 「API 和服務 → 憑證 → 建立憑證 → API 金鑰」，建立後點進去，**限制為只能用 Cloud Text-to-Speech API**，複製金鑰。

### 3. GitHub
1. 註冊 GitHub，建立一個 **Public（公開）** repository，例如 `morning-english`。
   （私人 repo 要付費方案才能用 Pages。公開 repo 代表音檔與 feed 網址是公開的，但別人不知道網址就找不到；金鑰存在 Secrets 裡，不會外洩。）
2. 把這個資料夾的所有檔案上傳到 repo。**注意 `.github/workflows/daily.yml` 的資料夾路徑要保持原樣。**

## 二、設定金鑰與選項

repo 的 **Settings → Secrets and variables → Actions**：

**Secrets（機密）**
- `ANTHROPIC_API_KEY`
- `GOOGLE_TTS_API_KEY`

**Variables（選填，不設就用預設值）**

| 名稱 | 用途 | 預設 |
|---|---|---|
| `KID_NAME` | 孩子的名字（開場會叫名字） | 不叫名字 |
| `WEATHER_LAT` / `WEATHER_LON` / `WEATHER_PLACE` | 天氣地點 | 新北市 |
| `GOOGLE_TTS_VOICE` | 語音名稱，例如 `en-US-Chirp3-HD-Leda` | Chirp3-HD-Leda |
| `SPEAKING_RATE` | 語速（0.25～2.0） | 0.92 |
| `SHOW_TITLE` | Podcast 名稱 | Morning English |
| `TTS_PROVIDER` | 改用 OpenAI 設 `openai`（另需 secret `OPENAI_API_KEY`） | google |

> 這是公開 repo，不想公開的資訊（孩子全名、精確地址）請不要放進去。名字用小名即可，天氣座標用城市等級即可。

## 三、第一次執行與測試

1. repo 的 **Actions** 分頁 → 選 **Daily English episode** → **Run workflow**。如果看到需要啟用 workflows 的提示，點同意。
2. 約 1～3 分鐘跑完（綠色勾勾）。失敗的話點進去看紅字訊息，GitHub 也會寄信通知你。
3. **Settings → Pages**：Source 選 **Deploy from a branch**，Branch 選 **gh-pages**、資料夾 **/(root)**，儲存。
4. 等 1～2 分鐘，開 `https://你的帳號.github.io/repo名稱/`，應該看到一個頁面，裡面有 feed 連結和今天的集數。

## 四、iPhone 訂閱

1. 打開「播客」App → 資源庫 → 右上角「⋯」→「依 URL 追蹤節目」，貼上
   `https://你的帳號.github.io/repo名稱/feed.xml`
   （選單名稱可能因 iOS 版本略有不同）
2. 進入這個節目，在節目設定裡開啟**自動下載新單集**。
3. 車上：有 CarPlay 就直接在播客 App 播放。只有藍牙的話，可以在「捷徑 → 自動化」建立「連接到汽車藍牙時 → 播放播客」，這個要實測一下你的 iOS 版本支援程度。

## 五、換聲音、試聽

在自己電腦上可以列出 Google 的語音清單，挑選後把名稱設進 `GOOGLE_TTS_VOICE`：

```bash
pip install -r requirements.txt
export GOOGLE_TTS_API_KEY="你的金鑰"
python generate.py --list-voices
```

建議挑 2～3 種聲音各產一集（手動執行 workflow 前先改變數），在車上放給孩子聽，選他喜歡的。

本機測試（不呼叫任何付費 API，只產生靜音檔驗證流程）：`python generate.py --dry-run`

## 六、日常維護與注意事項

- **只保留最近 30 集**，舊的自動刪除。想調整改 `KEEP_EPISODES`。
- **GitHub 排程會延遲**：偶爾晚幾分鐘到更久，所以設在 5:30，給你出門前留緩衝。
- **60 天不動會被停用**：GitHub 對「公開 repo 長期沒有活動」的排程可能會自動停用，並寄信通知。收到的話到 Actions 分頁點重新啟用即可。
- **新聞內容的限制**：BBC 的 RSS 只提供標題和短摘要，所以新聞段落只會重述摘要裡有的資訊，不會補細節，這是刻意的，避免 AI 編造。內容也建議你偶爾抽聽幾集確認難度與適合度。
- **單字重複**：程式會記錄最近教過的單字，要求 Claude 避開。
- **如果 Google 語音不接受語速設定**，程式會自動不帶語速重試。
- **如果 feed 沒更新**：先確認 Actions 有綠勾、Pages 設定的是 gh-pages 分支；播客 App 本身重新整理 feed 的頻率不一，晚幾小時屬正常。
- **新聞來源標示**：每集在新聞段落結尾會口頭標明來自 BBC Newsround，請保持這個設定，也請只做個人家用。
