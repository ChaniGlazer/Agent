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
| `agent/tools.py` | מימוש 16 ה-Tools, dry-run, approval, retry/recovery |
| `agent/llm.py` | הפשטה לספקי LLM (OpenAI/Anthropic + כל ספק תואם-OpenAI - ראו טבלה למטה) |
| `agent/router.py` | Model Router: ניתוב לפי סוג צעד, תקציבי שימוש, ו-fallback על Rate Limit |
| `agent/prompts.py` | תבניות הפרומפט למודל |
| `agent/page_state.py` | מבנה הנתונים `PageState` שמתאר את הדף ל-LLM |
| `agent/memory.py` | זיכרון המשימה: יעד, היסטוריה, שגיאות |
| `agent/logger.py` | הגדרת logging + רישום פעולות מובנה |
| `agent/utils.py` | `ActionResult`, `Timer`, `extract_json` |
| `extension/background.js` | חיבור ה-WebSocket, ניתוב פעולות (navigate/refresh/screenshot/upload/download), אישורים |
| `extension/content.js` | ביצוע בפועל ב-DOM (click/fill/read/wait/scroll/press/tap/type) וקריאת מצב הדף |
| `extension/popup.html/js` | ממשק: הגדרת המשימה, מצבי בטיחות, לוג חי |
| `extension/options.html/js` | הגדרת כתובת השרת וה-token |

## דרישות מוקדמות

* Python 3.12 ומעלה (לשרת).
* Google Chrome (להתקנת התוסף).
* מפתח API לספק ה-LLM שנבחר (יש גם מסלולים חינמיים - ראו טבלת הספקים למטה).
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
llm_provider: "openai"          # ראו טבלת ספקים למטה
model_name: "gpt-4o-mini"       # לדוגמה: "deepseek-chat" עבור deepseek
approval_mode: "none"           # none | each_action | final_only
dry_run: false
```

מפתחות/סודות מומלץ להעביר דרך משתני סביבה, לא לשמור בקובץ (וזה גם בדיוק
איך שמגדירים אותם ב-Render):

```bash
export AGENT_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export OPENAI_API_KEY="sk-..."
```

`TARGET_URL`, `LLM_PROVIDER` ו-`MODEL_NAME` ניתנים גם הם לדריסה ממשתני
סביבה, כך שאפשר להריץ את השרת מבלי לערוך את `config.yaml` בכלל (רלוונטי
במיוחד לפריסה ב-Render, ראו למטה).

### ספקי LLM נתמכים (כולל מסלולים חינמיים)

מלבד OpenAI ו-Anthropic (שמשתמשים בספריות ה-SDK הרשמיות שלהם), כל ספק אחר
נתמך דרך endpoint תואם-OpenAI (`agent.llm.OpenAICompatibleProvider`) -
אותה ספריית `openai` שכבר מותקנת, מכוונת לכתובת אחרת. לרוב הספקים יש
כתובת ברירת מחדל מובנית; רק Cloudflare דורש שתגדירו את `base_url` בעצמכם
(הכתובת שלו כוללת את ה-account ID שלכם).

| `provider` | משתנה סביבה למפתח | מסלול חינמי? | הערות |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | לא | |
| `anthropic` | `ANTHROPIC_API_KEY` | לא | |
| `deepseek` | `DEEPSEEK_API_KEY` | לפעמים זול מאוד | |
| `google` | `GOOGLE_API_KEY` | כן (Gemini Flash) | דרך Google AI Studio |
| `groq` | `GROQ_API_KEY` | כן | תגובות מהירות מאוד, מכסות נמוכות יחסית |
| `together` | `TOGETHER_API_KEY` | קרדיט התחלתי | אחר כך בתשלום |
| `openrouter` | `OPENROUTER_API_KEY` | חלקית | רק מודלים עם סיומת `:free` בשם |
| `huggingface` | `HF_TOKEN` | כן (מוגבל) | דרך ה-router המאוחד של Inference Providers |
| `mistral` | `MISTRAL_API_KEY` | קרדיט/מוגבל | |
| `cohere` | `COHERE_API_KEY` | מסלול ניסיון | |
| `cloudflare` | `CLOUDFLARE_API_TOKEN` | כן (מכסת CPU) | **חובה** `base_url` עם ה-account ID שלכם |
| `nvidia` | `NVIDIA_API_KEY` | לעיתים קרדיט | דרך NIM |

> **חשוב**: מכסות ותנאי החינם משתנים לעיתים קרובות - בדקו בדף התמחור
> הרשמי של כל ספק לפני הסתמכות רצינית על מסלול חינמי.

### שילוב מודלים (Multi-Model Routing) - כולל Fallback חינמי

במקום מודל יחיד, אפשר להגדיר **מאגר מודלים** עם ניתוב אוטומטי לפי התאמת
משימה ולפי מגבלות שימוש - כולל שרשור בין כמה ספקים חינמיים, כשכל אחד
מתמלא הבקשה עוברת אוטומטית לבא בתור. מוסיפים ל-`config.yaml` רשימת
`models` (שמחליפה את `llm_provider`/`model_name`):

```yaml
models:
  - provider: "google"
    model_name: "gemini-2.5-flash"
    tasks: ["simple", "complex"]
    priority: 1
    max_requests_per_minute: 10
    max_requests_per_day: 250
  - provider: "groq"
    model_name: "llama-3.3-70b-versatile"
    tasks: ["simple"]              # מודל מהיר לצעדים שגרתיים
    priority: 2
    max_requests_per_minute: 30
  - provider: "openrouter"
    model_name: "meta-llama/llama-3.1-8b-instruct:free"
    tasks: ["simple", "complex"]
    priority: 3
  - provider: "deepseek"           # רשת ביטחון זולה בתשלום, כדי שהמשימה לא תיתקע
    model_name: "deepseek-chat"
    tasks: ["simple", "complex"]
    priority: 9
```

(דוגמה מורחבת יותר, עם עוד ספקים, נמצאת ב-`config.yaml.example`.)

איך הניתוב עובד (`agent/router.py`):

* **התאמת משימה**: כל צעד מסווג אוטומטית - `complex` אם זה הצעד הראשון
  במשימה (נדרש תכנון) או אם אחת משלוש הפעולות האחרונות נכשלה (נדרשת
  התאוששות); אחרת `simple`. הצעד מנותב למודל זמין שמוגדר לסוג הזה, לפי
  `priority` (נמוך = מועדף). אם אין מודל מתאים זמין - נבחר מודל זמין אחר,
  כדי שהמשימה לא תיעצר.
* **מגבלות שימוש**: לכל מודל נשמר מונה בקשות בחלון של 60 שניות ומונה
  יומי. מודל שמיצה את התקציב מדולג עד שהחלון מתפנה, והתנועה עוברת למודל
  הבא.
* **Fallback על Rate Limit**: אם ספק מחזיר שגיאת קיבולת (HTTP 429 או 529),
  המודל "יושב על הספסל" למשך `cooldown_seconds` (ברירת מחדל: 60) והבקשה
  מנותבת מיד למודל הבא. שגיאות אחרות (בקשה שגויה וכו') לא גוררות fallback.
* אם **כל** המודלים חסומים/מוצו, המשימה נעצרת בצורה מסודרת עם
  `stop_reason: no_model_available` שמדווח לתוסף.

מודל יחיד ללא מגבלות ממשיך לעבוד בדיוק כמו קודם (בלי router בכלל).

### הרצה מקומית

```bash
python -m agent.main --config config.yaml
```

השרת עולה על `http://localhost:8000` (או הפורט שהוגדר), עם:

* `GET /healthz` - בדיקת חיות.
* `GET /` - דף נחיתה ציבורי (`agent/static/index.html`) עם מיתוג כללי של החברה;
  לא חושף שום מידע על מה שהשרת בפועל עושה (אין JSON, אין `target_url`).
* `WS /ws?token=...` - ה-endpoint שהתוסף מתחבר אליו.

### פריסה ל-Render

השירות מוגדר כ-**Web Service** של Render (לא Background Worker / Static
Site / Cron Job) - זה השירות היחיד מסוגי Render שחושף כתובת ציבורית
(`https://`/`wss://`) שהתוסף יכול להתחבר אליה, וגם התומך ב-WebSocket
(שעליו כל התקשורת עם התוסף מתבססת) ובבדיקת חיות (`healthCheckPath`).

**אפשרות א' - Blueprint (מומלץ, אוטומטי):** בריפו קיים קובץ `render.yaml`
שכבר מגדיר את השירות עם כל השדות הנדרשים ל-Web Service (`type: web`,
`runtime: python`, `buildCommand`, `startCommand`, `healthCheckPath`):

1. ב-Render: **New → Blueprint**, חברו את הריפו הזה.
2. Render יזהה את `render.yaml` וייצור שירות מסוג Web Service בשם `web-agent`.
3. לאחר הפריסה הראשונה, בכרטיסייה **Environment** של השירות הגדירו:
   * `TARGET_URL` - כתובת האתר הפנימי.
   * `AGENT_AUTH_TOKEN` - סוד משותף (ייצרו עם `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
   * `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` - לפי הספק שנבחר (`LLM_PROVIDER`, גם הוא ניתן לעריכה בכרטיסיית Environment).
4. Render יפרוס מחדש אוטומטית. כתובת ה-WebSocket שלכם תהיה
   `wss://<שם-השירות>.onrender.com/ws`.

**אפשרות ב' - יצירה ידנית של Web Service** (אם מעדיפים לא להשתמש ב-Blueprint):

1. ב-Render: **New → Web Service**, חברו את הריפו הזה.
2. בטופס ההגדרה:
   * **Language/Runtime**: Python 3
   * **Build Command**: `pip install -r requirements.txt && cp -n config.yaml.example config.yaml`
   * **Start Command**: `python -m agent.main --config config.yaml`
   * **Health Check Path** (בהגדרות מתקדמות): `/healthz`
   * **Instance Type**: כל תוכנית שאינה Static - `Starter` מספיק להתחלה.
3. תחת **Environment**, הוסיפו את אותם משתני הסביבה שמפורטים באפשרות א'
   (`TARGET_URL`, `AGENT_AUTH_TOKEN`, `LLM_PROVIDER`, `MODEL_NAME`, ומפתח
   ה-API הרלוונטי).
4. Render יבנה ויפרוס את השירות ויקצה לו כתובת `https://<שם-השירות>.onrender.com`
   (והתוסף מתחבר ל-`wss://<אותה-כתובת>/ws`).

בשני המקרים אין צורך להגדיר `PORT` ידנית - Render מזריק אותו אוטומטית
כמשתנה סביבה, ו-`agent/main.py` קורא אותו ומאזין עליו (ראו `agent/main.py`).
השרת אינו תלוי ב-Playwright ואינו זקוק לדפדפן מותקן ב-Render - זו בדיוק
הסיבה שהוא יכול לרוץ על Web Service רגיל בלי שום תצורה מיוחדת.

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

### דוגמה: טיפול בדף פניות

משימה כמו "עברי על הפניות הפתוחות ובדקי את הקישורים שבתוכן" מתבצעת אוטומטית
בעזרת `open_tab`/`close_tab`/`check_links` בלי צורך בהגדרה נוספת - רק כותבים
את זה בשפה חופשית בפופאפ, למשל:

> "עברי על כל הפניות בעמוד שמסומנות כ'לא טופלו', פתחי כל אחת בכרטיסייה
> חדשה, בדקי אם הקישורים בתוכה תקינים, וסגרי אותה לפני שעוברים לבאה."

הסוכן ילולאה כך על כל פנייה: יזהה מהעמוד הראשי (לפי הטקסט של כל שורה/קישור
ב-PAGE STATE) אילו פניות מסומנות כלא-מטופלות → `open_tab` על הקישור שלה →
`check_links` בתוך הפנייה → `close_tab` בחזרה לרשימה → הפנייה הבאה. ברגע
שכל הפניות טופלו, הוא מסמן `finished: true` עם סיכום (כמה פניות נבדקו, אילו
נמצאו בהן קישורים שבורים) ב-`reason` שלו, שנרשם בלוג.

**טיפ**: כדאי להתחיל עם מצב אישור **"אישור לפני כל פעולה"** בהרצה הראשונה
על דף פניות חדש, כדי לוודא שהסוכן מזהה נכון אילו פניות "לא טופלו" (התלוי
לגמרי בטקסט/עיצוב שהאתר שלכם משתמש בו לסימון סטטוס) - ורק אז לעבור למצב
אוטונומי.

## מצבי בטיחות

* **Dry Run** - פעולות משנות-מצב (click, fill, press, navigate, upload,
  download, refresh, scroll, open_tab, close_tab, tap, type) רק נרשמות ומדווחות, לא
  מבוצעות בפועל. פעולות קריאה (`read`, `wait`, `screenshot`, `check_links`)
  עדיין רצות כדי לתת למודל הקשר אמיתי.
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
| `scroll(selector?, text?)` | גלילה לאלמנט (עם `selector`), או גלילת עמוד לפי כיוון (`up`/`down`/`left`/`right`), כמות פיקסלים מספרית, או ברירת מחדל ללא `text` | `content.js` |
| `press(key)` | לחיצת מקלדת | `content.js` |
| `tap(text)` | הקשה על נקודה בעמוד לפי קואורדינטות `"x,y"` (מ-PAGE STATE) - fallback לכשאין סלקטור אמין | `content.js` |
| `type(text)` | הקלדת טקסט תו-אחר-תו לתוך האלמנט הממוקד כרגע (אחרי `click`/`tap`) | `content.js` |
| `navigate(url)` | ניווט לכתובת | `background.js` (`chrome.tabs.update`) |
| `refresh()` | רענון הדף | `background.js` (`chrome.tabs.reload`) |
| `screenshot()` | צילום החלון הנראה (viewport) - **לא** עמוד מלא | `background.js` (`chrome.tabs.captureVisibleTab`) |
| `upload(file)` | העלאת קובץ מקומי ל-`<input type=file>` | `background.js` דרך `chrome.debugger` |
| `download()` | לחיצה שמפעילה הורדה | `background.js` + `content.js` |
| `open_tab(selector)` | פותח את הפריט שמאחורי הסלקטור **בכרטיסייה חדשה**, והופך אותה לכרטיסייה הפעילה מהצעד הבא | `background.js` (`chrome.tabs.create` או לחיצה אמיתית + `chrome.tabs.onCreated`) |
| `close_tab()` | סוגר את הכרטיסייה שנפתחה לאחרונה עם `open_tab` וחוזר לכרטיסייה הקודמת | `background.js` (`chrome.tabs.remove`) |
| `check_links(selector?)` | סורק קישורים (בתוך `selector` אם ניתן, אחרת בכל העמוד) ובודק אם כל אחד מהם תקין (מבצע בקשת רשת ובודק קוד תגובה) | `background.js` (`fetch`) + `content.js` (איסוף הקישורים) |

**איך `open_tab`/`close_tab` עובדים**: הסוכן עוקב אחרי "כרטיסייה פעילה" יחידה
בכל רגע נתון. `open_tab` קודם מנסה לפתור קישור אמיתי (`<a href>`) מאחורי
הסלקטור ולפתוח אותו ישירות. **אם אין קישור** - נפוץ מאוד בדפי רשימות/פניות
שבהם כל שורה היא `<div>`/`<tr>` עם JS `onclick` ולא תגית `<a>` - הוא מבצע
**לחיצה אמיתית** על האלמנט (בדיוק כמו הקשה בעכבר) וממתין עד 6 שניות
לכרטיסייה חדשה שתיפתח כתוצאה מהלחיצה; אם אחת נפתחת, היא הופכת לפעילה. בכל
מקרה, הכרטיסייה הקודמת נשמרת בערימה - כל הפעולות הבאות (`read`, `click`,
`check_links` וכו') פועלות על הכרטיסייה החדשה, עד ש-`close_tab` סוגר אותה
וחוזר לקודמת. כך אפשר לעבור פריט-פריט ברשימת פניות: `open_tab` על השורה →
`check_links` (או כל בדיקה אחרת) בתוך הפנייה → `close_tab` בחזרה לרשימה →
`open_tab` על הפנייה הבאה.

**איך `check_links` עובד**: `content.js` אוסף את כל תגיות ה-`<a href>`
בתחום שהוגדר (מדלג על `#`, `javascript:`, `mailto:`, `tel:`), ו-`background.js`
שולח לכל קישור בקשת HTTP (`HEAD`, ועם fallback ל-`GET`) כדי לבדוק אם הוא
מחזיר תשובה תקינה. התוצאה - מספר הקישורים שנבדקו, כמה תקינים וכמה שבורים
(עם הסיבה: קוד HTTP או timeout) - מוצגת גם בהודעה הקצרה שהמודל רואה בצעד
הבא, כדי שיוכל להחליט מה לדווח.

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
* `tests/test_router.py` - ה-Model Router: סיווג צעדים, ניתוב לפי התאמה
  ועדיפות, תקציבי שימוש (דקה/יום), ו-fallback עם cooldown על Rate Limit.
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
│   ├── tools.py             # ToolExecutor - 16 ה-Tools
│   ├── llm.py                # ספקי LLM (OpenAI/Anthropic + Provider תואם-OpenAI)
│   ├── router.py             # Model Router - שילוב מודלים
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
