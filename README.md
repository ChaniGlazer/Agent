# Web Agent (Server + Chrome Extension)

סוכן בינה מלאכותית שפועל אך ורק על אתר אינטרנט פנימי אחד. ה"מוח" של הסוכן
(הלולאה הראשית + קריאות ל-LLM) רץ כשרת Python בענן (למשל ב-Render), ואילו
ביצוע הפעולות בפועל בדפדפן - קריאת הדף, לחיצות, מילוי טפסים - מתבצע על ידי
תוסף Chrome שמותקן על המחשב שלכם ומדבר עם השרת דרך WebSocket.

השרת **לא** נוגע בדפדפן בעצמו ואין בו תלות ב-Playwright; כל הפעולות
מבוצעות על ידי התוסף מול הטאב שפתוח אצלכם.

## ארכיטקטורה

```
                         WebSocket (wss://.../ws?token=...)
┌─────────────────────┐  ◄──────────────────────────────►  ┌─────────────────────┐
│   שרת (Render)        │                                    │   תוסף Chrome         │
│   agent/server.py     │   {"type":"request", action:...}   │   background.js       │
│   Controller ←→ LLM   │ ─────────────────────────────────► │   ↓ מבצע ב-DOM        │
│   ToolExecutor        │ ◄───────────────────────────────── │   content.js          │
│                       │   {"type":"response", payload:...} │                       │
└─────────────────────┘                                    └─────────────────────┘
                                                                       │
                                                                       ▼
                                                              הטאב הפתוח אצלכם
                                                              (target_url בלבד)
```

לולאת העבודה:

```
Controller
   ↓ שולח בקשת get_page_state לתוסף
LLM
   ↓ מחזיר JSON: פעולה יחידה לביצוע
ToolExecutor
   ↓ Approval? Dry-run? → בקשת execute_action לתוסף → התוסף מבצע ב-DOM
בדיקת הצלחה
   ↓ נכשל? → Retry → בקשת refresh לתוסף → צילום מסך → Log
חזרה ל-LLM (repeat) עד finished=true
```

| קובץ | תפקיד |
|---|---|
| `agent/main.py` | נקודת כניסה: מפעיל את שרת ה-`uvicorn` |
| `agent/server.py` | אפליקציית FastAPI, ה-endpoint `/ws`, פרוטוקול ההודעות |
| `agent/config.py` | טעינת `config.yaml` + משתני סביבה ל-`AgentConfig` |
| `agent/connection.py` | `ExtensionConnection`/`RemoteBrowser` - התאמת בקשה↔תשובה מול התוסף |
| `agent/controller.py` | הלולאה הראשית: read state → LLM → act → verify |
| `agent/tools.py` | מימוש 11 ה-Tools, dry-run, approval, retry/recovery |
| `agent/llm.py` | הפשטה לספקי LLM (OpenAI / Anthropic / DeepSeek) |
| `agent/prompts.py` | תבניות הפרומפט למודל |
| `agent/page_state.py` | מבנה הנתונים `PageState` שמתאר את הדף ל-LLM |
| `agent/memory.py` | זיכרון המשימה: יעד, היסטוריה, שגיאות |
| `agent/logger.py` | הגדרת logging + רישום פעולות מובנה |
| `agent/utils.py` | `ActionResult`, `Timer`, `extract_json` |
| `extension/background.js` | חיבור ה-WebSocket, ניתוב פעולות (navigate/refresh/screenshot/upload/download), אישורים |
| `extension/content.js` | ביצוע בפועל ב-DOM (click/fill/read/wait/scroll/press) וקריאת מצב הדף |
| `extension/popup.html/js` | ממשק: הגדרת המשימה, מצבי בטיחות, לוג חי |
| `extension/options.html/js` | הגדרת כתובת השרת וה-token |

## דרישות מוקדמות

* Python 3.12 ומעלה (לשרת).
* Google Chrome (להתקנת התוסף).
* מפתח API לספק ה-LLM שנבחר (OpenAI, Anthropic או DeepSeek).
* חשבון Render (או כל שרת אחר שיודע להריץ אפליקציית ASGI) לפריסת השרת - אפשר גם להריץ מקומית לבדיקות.

## חלק 1: השרת

### התקנה מקומית

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### קונפיגורציה

```bash
cp config.yaml.example config.yaml
```

ערכו את `config.yaml` (או, מומלץ יותר לפריסה, השתמשו במשתני סביבה - ראו בהמשך):

```yaml
target_url: "https://internal.example.local"
llm_provider: "openai"          # openai | anthropic | deepseek
model_name: "gpt-4o-mini"       # לדוגמה: "deepseek-chat" עבור deepseek
approval_mode: "none"           # none | each_action | final_only
dry_run: false
```

מפתחות/סודות מומלץ להעביר דרך משתני סביבה, לא לשמור בקובץ (וזה גם בדיוק
איך שמגדירים אותם ב-Render):

```bash
export AGENT_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export OPENAI_API_KEY="sk-..."
# או
export ANTHROPIC_API_KEY="sk-ant-..."
# או
export DEEPSEEK_API_KEY="sk-..."
```

`TARGET_URL`, `LLM_PROVIDER` ו-`MODEL_NAME` ניתנים גם הם לדריסה ממשתני
סביבה, כך שאפשר להריץ את השרת מבלי לערוך את `config.yaml` בכלל (רלוונטי
במיוחד לפריסה ב-Render, ראו למטה).

> **DeepSeek**: ה-API של DeepSeek תואם ל-OpenAI (`https://api.deepseek.com`),
> ולכן משתמש באותה ספריית `openai` שכבר מותקנת - אין צורך בחבילה נוספת.

### הרצה מקומית

```bash
python -m agent.main --config config.yaml
```

השרת עולה על `http://localhost:8000` (או הפורט שהוגדר), עם:

* `GET /healthz` - בדיקת חיות.
* `WS /ws?token=...` - ה-endpoint שהתוסף מתחבר אליו.

### פריסה ל-Render

בריפו קיים קובץ `render.yaml` (Blueprint) שמגדיר Web Service מוכן:

1. ב-Render: **New → Blueprint**, חברו את הריפו הזה.
2. Render יזהה את `render.yaml` וייצור שירות בשם `web-agent`.
3. לאחר הפריסה הראשונה, בכרטיסייה **Environment** של השירות הגדירו:
   * `TARGET_URL` - כתובת האתר הפנימי.
   * `AGENT_AUTH_TOKEN` - סוד משותף (ייצרו עם `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
   * `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` - לפי הספק שנבחר (`LLM_PROVIDER`, גם הוא ניתן לעריכה בכרטיסיית Environment).
4. Render יפרוס מחדש אוטומטית. כתובת ה-WebSocket שלכם תהיה
   `wss://<שם-השירות>.onrender.com/ws`.

השרת אינו תלוי ב-Playwright ואינו זקוק לדפדפן מותקן ב-Render - זו בדיוק
הסיבה שהוא יכול לרוץ שם בקלות.

## חלק 2: תוסף ה-Chrome

התוסף נמצא בתיקיית `extension/` ואינו פורסם בחנות - מתקינים אותו כתוסף
"טעון שלא ארוז" (unpacked).

### התקנה

1. פתחו `chrome://extensions`.
2. הפעילו **מצב מפתח** (Developer mode) בפינה הימנית העליונה.
3. **Load unpacked** → בחרו את התיקייה `extension/`.
4. לחצו על אייקון התוסף → **Server settings** (או קליק ימני על האייקון → Options).
5. הזינו:
   * **Server URL**: כתובת השרת, לדוגמה `wss://web-agent.onrender.com` (בלי `/ws` בסוף).
   * **Auth token**: אותו `AGENT_AUTH_TOKEN` שהגדרתם בשרת.
6. שמרו. התוסף יתחבר אוטומטית; נורית הסטטוס בפופאפ תהפוך לירוקה.

### שימוש

1. פתחו טאב באתר הפנימי (`target_url`) - או פשוט הריצו משימה והתוסף יפתח
   טאב חדש אם לא נמצא טאב מתאים.
2. לחצו על אייקון התוסף.
3. כתבו את המשימה בשפה חופשית, בחרו מצב אישור ו/או Dry Run, ולחצו **Start task**.
4. עקבו אחרי ההתקדמות בלוג החי בפופאפ.

## מצבי בטיחות

* **Dry Run** - פעולות משנות-מצב (click, fill, press, navigate, upload,
  download, refresh, scroll) רק נרשמות ומדווחות, לא מבוצעות בפועל. פעולות
  קריאה (`read`, `wait`, `screenshot`) עדיין רצות כדי לתת למודל הקשר אמיתי.
* **Approval Mode**:
  * `none` - ריצה אוטונומית מלאה.
  * `each_action` - לפני **כל** פעולה נשלחת התראת מערכת (notification) עם
    כפתורי Approve/Deny; אם אין תגובה תוך שתי דקות, הפעולה נדחית אוטומטית.
  * `final_only` - התראת אישור נשלחת רק לפני הפעולה שבה המודל מסמן
    `finished: true` (הצעד המסיים את המשימה).
* אם המודל אינו בטוח או שחסר מידע, הוא מחזיר `action: "ask"` והסוכן עוצר
  ומדווח שנדרשת החלטה אנושית, במקום לנחש.
* המודל מחויב להשתמש רק בסלקטורים שמופיעים בפועל במצב הדף שסופק לו -
  לעולם לא "ממציא" סלקטור.
* ניתן לעצור משימה רצה בכל שלב מהפופאפ (**Stop**) - הסוכן יעצור לפני
  הפעולה הבאה שלו.

## Error Recovery

לכל פעולה: ניסיון → אם נכשל, ניסיון חוזר (`retry_count` פעמים) → אם עדיין
נכשל, בקשת רענון דף מהתוסף וניסיון נוסף → אם עדיין נכשל, בקשת צילום מסך
מהתוסף ורישום שגיאה מובנית ל-log ול-memory, והחזרת תוצאה ברורה ללולאה
הראשית (שמעבירה את הכישלון בחזרה למודל בצעד הבא, כדי שינסה גישה חלופית).

## כלי הפעולה (Tools)

| Tool | תיאור | היכן מתבצע |
|---|---|---|
| `click(selector)` | לחיצה על אלמנט לפי סלקטור | `content.js` |
| `fill(selector, text)` | מילוי שדה טקסט | `content.js` |
| `read(selector)` | קריאת תוכן/ערך של אלמנט | `content.js` |
| `wait(selector)` | המתנה לאלמנט (עד שנראה) | `content.js` |
| `scroll()` | גלילה לאלמנט או גלילת עמוד | `content.js` |
| `press(key)` | לחיצת מקלדת | `content.js` |
| `navigate(url)` | ניווט לכתובת | `background.js` (`chrome.tabs.update`) |
| `refresh()` | רענון הדף | `background.js` (`chrome.tabs.reload`) |
| `screenshot()` | צילום החלון הנראה (viewport) - **לא** עמוד מלא | `background.js` (`chrome.tabs.captureVisibleTab`) |
| `upload(file)` | העלאת קובץ מקומי ל-`<input type=file>` | `background.js` דרך `chrome.debugger` |
| `download()` | לחיצה שמפעילה הורדה | `background.js` + `content.js` |

### מגבלות ידועות (נובעות מ-API-ים של תוספי דפדפן)

* **`screenshot`**: `chrome.tabs.captureVisibleTab` מצלם רק את החלון
  הנראה כרגע, לא עמוד שלם עם גלילה (בניגוד ל-Playwright המקומי).
* **`upload`**: ה-`text`/`file` שמועבר חייב להיות **נתיב מוחלט קיים על
  המחשב שמריץ את הדפדפן**. הביצוע דרך `chrome.debugger` (אותו מנגנון
  ש-Playwright עצמו משתמש בו), ולכן בזמן ההעלאה Chrome מציג שורת התראה
  "מתבצע דיבוג של הדפדפן" - היא נעלמת מיד בסיום.
* **`download`**: הקובץ שיורד נשמר בתיקיית ה-Downloads המקומית של
  המשתמש; השרת **לא** מקבל גישה לתוכן הקובץ, רק לאישור שההורדה החלה
  ולשם הקובץ המוצע.

## בדיקות

```bash
python -m pytest tests -q
```

כל הבדיקות רצות ללא דפדפן אמיתי, ללא WebSocket אמיתי וללא קריאת LLM
אמיתית:

* `tests/test_utils.py`, `test_memory.py`, `test_config.py`, `test_llm.py` -
  בדיקות יחידה סטנדרטיות.
* `tests/test_connection.py` - התאמת בקשה↔תשובה ב-`ExtensionConnection`
  מול טרנספורט מדומה.
* `tests/test_tools.py` - `ToolExecutor` (dry-run, approval, retry/refresh/
  screenshot) מול `RemoteBrowser` מדומה.
* `tests/test_controller.py` - לולאת ה-`AgentController` מול LLM מתוסרט.
* `tests/integration/test_server_flow.py` - בדיקת אינטגרציה מלאה: מפעילה
  את אפליקציית ה-FastAPI האמיתית (`TestClient`), מדמה תוסף שמגיב על גבי
  WebSocket אמיתי (בתוך התהליך), ומוודאת שפרוטוקול ההודעות המלא - כולל
  אימות token, `hello_ack`, `get_page_state`/`execute_action`/`response`,
  ו-`task_finished` - עובד קצה-לקצה.

## אבטחה

* השרת דוחה כל חיבור WebSocket שאינו נושא `token` תואם ל-`AGENT_AUTH_TOKEN`.
* התוסף פועל אך ורק מול `target_url` שהשרת מדווח עליו ב-handshake; הוא
  אינו יוזם פעולות על טאבים אחרים.
* לחיצות מתבצעות תמיד לפי סלקטור ב-DOM, לא לפי קואורדינטות עכבר.
* קריאת תוכן העמוד מתבצעת דרך ה-DOM (`innerText`/`value`), ללא OCR.
* המודל אינו מבצע פעולה שלא התבקשה, ועוצר ומבקש החלטה אנושית כשחסר מידע.
* מפתחות API וה-`auth_token` נטענים ממשתני סביבה ולא נשמרים בברירת מחדל
  בקובץ קונפיגורציה (`config.yaml` נמצא ב-`.gitignore`).
* תוסף הדפדפן מבקש הרשאת `<all_urls>` כדי שיוכל לפעול על כל `target_url`
  שתגדירו מבלי לארוז מחדש את התוסף - אך בפועל הוא פועל רק מול הכתובת
  שהשרת שלכם מדווח עליה.

## מבנה הפרויקט

```
Agent/
├── agent/
│   ├── main.py              # הפעלת שרת ה-uvicorn
│   ├── server.py            # FastAPI, endpoint /ws, פרוטוקול ההודעות
│   ├── config.py            # AgentConfig + טעינת YAML/משתני סביבה
│   ├── connection.py        # ExtensionConnection / RemoteBrowser
│   ├── controller.py        # לולאת ה-Agent הראשית
│   ├── tools.py             # ToolExecutor - 11 ה-Tools
│   ├── llm.py                # ספקי LLM (OpenAI/Anthropic/DeepSeek)
│   ├── prompts.py            # תבניות פרומפט
│   ├── page_state.py         # מבנה הנתונים PageState
│   ├── memory.py              # זיכרון משימה
│   ├── logger.py              # הגדרת logging
│   ├── utils.py                # ActionResult, extract_json
│   ├── screenshots/            # צילומי מסך (נוצר אוטומטית)
│   ├── logs/                    # קבצי log (נוצר אוטומטית)
│   └── data/                     # memory.json
├── extension/
│   ├── manifest.json
│   ├── background.js          # WebSocket + ניתוב פעולות ברמת הטאב
│   ├── content.js              # DOM actions + קריאת מצב דף
│   ├── storage.js               # עטיפת chrome.storage משותפת
│   ├── popup.html / popup.js     # ממשק המשימה
│   ├── options.html / options.js  # הגדרת שרת + token
│   └── icons/
├── tests/
│   ├── test_*.py                  # בדיקות יחידה
│   └── integration/
│       └── test_server_flow.py
├── render.yaml                # Render Blueprint
├── config.yaml.example
├── requirements.txt
└── README.md
```
