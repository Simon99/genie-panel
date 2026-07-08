# genie-panel

影片批次處理面板:雙擊開啟的 Web UI,掃描 `~/Movies/*.mp4` 顯示每部的
筆記(genie-transcript)與圖文 PDF(genie-vid2pdf)處理狀態,支援排程
佇列、暫停/繼續/重啟、時間範圍一鍵排程與剩餘時間預估。

## 需求

- macOS + `ffmpeg`(`/opt/homebrew/bin`)
- `~/proj_genie/.venv`:flask + mlx-whisper(genie-live 的環境即可)
- 同層 checkout:genie-core、genie-transcript、genie-vid2pdf
- LM Studio 跑一顆文字模型(筆記的 LLM 結構化)

## 安裝與啟動

```bash
cd ~/proj_genie && git clone https://github.com/Simon99/genie-panel.git
# 建立 Finder 捷徑(可選):
cp genie-panel/影片處理面板.command ~/Movies/genie-notes/
```

之後雙擊 `影片處理面板.command`(repo 內或捷徑皆可),瀏覽器自動開
`http://127.0.0.1:5250`。已在執行時再次雙擊只會開頁面。更新面板:
`git -C ~/proj_genie/genie-panel pull` 後重啟。

## 功能

- **狀態表**:每部影片的筆記 / 圖文 PDF 狀態(完成/未處理/排程中/處理中/失敗,
  失敗滑鼠停留看原因),完成的直接連到 notes.html / slides.pdf
- **一鍵排程**:近一月 / 近一季 / 近半年(只認檔名 `YYYY-MM-DD` 前綴;無日期
  檔名只經「全部」或單部按鈕)/ 全部;按鈕上顯示「幾部 · 預估總時長」
- **佇列**:由上而下依序執行;每項可置頂/上移/下移/移除;第一條是進行中任務,
  可 ⏸ 暫停(SIGSTOP 整個 process group,GPU 立即釋放)/ ▶ 繼續 / 重啟 / 中止
- **預估**:筆記 ≈ 片長 12%、圖文 PDF ≈ 片長 9%(實測值,見 dashboard.py 常數)
- **轉寫引擎切換**:
  - **本地**(mlx-whisper medium):音訊不離開機器,約 25× 實時,佔 GPU
  - **Groq 雲端**(whisper-large-v3):約 100× 實時、嘈雜音源明顯更準,不佔 GPU,
    但**音訊會上傳**。首次選用會彈框要求 API 金鑰,即時驗證後存入 `~/.env`
    (權限 600),日後自動沿用;免費額度 8 小時音訊/日,面板顯示今日用量。
    **額度用盡/限流/斷線時自動改用本地**完成該部(金鑰無效則直接報錯)
- **INDEX**:每完成一部筆記自動重建 `~/Movies/genie-notes/INDEX.md/html` 總覽

## 行為說明

- 狀態以磁碟為準(`structured.json` / `slides.pdf` 存在即完成),面板重啟不影響
- 「清空佇列」不中斷進行中任務;要全停:先清空佇列,再按「中止」
- 面板結束(Ctrl-C / 關 Terminal)會終止進行中的子程序,不留孤兒
- 只綁 127.0.0.1
- Groq 金鑰只寫入 `~/.env`,不進 repo、不回傳頁面(前端只顯示「已設定」)
