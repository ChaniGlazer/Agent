# Web Agent (Playwright)

סוכן בינה מלאכותית ב-Python, מיועד לעבוד אך ורק על אתר אינטרנט פנימי אחד.
הסוכן מתחבר לדפדפן Chrome קיים (Remote Debugging), קורא את מצב הדף מה-DOM,
מעביר אותו למודל שפה שמחליט על הפעולה הבאה, מבצע אותה באמצעות Playwright,
בודק הצלחה, ומתאושש משגיאות - עד שהמשימה הושלמה.

## ארכיטקטורה

```
Controller
   ↓ קורא את מצב הדף (DOM)
LLM
   ↓ מחזיר JSON: פעולה יחידה לביצוע
ToolExecutor
   ↓ Approval? Dry-run? → Playwright
בדיקת הצלחה
   ↓ נכשל? → Retry → Refresh → Screenshot → Log
חזרה ל-LLM (repeat) עד finished=true
```

| קובץ | תפקיד |
|---|---|
| `agent/main.py` | נקודת כניסה (CLI) |
| `agent/config.py` | טעינת `config.yaml` ל-`AgentConfig` |
| `agent/browser.py` | חיבור ל-Chrome קיים דרך CDP, בחירת Tab |
| `agent/controller.py` | הלולאה הראשית: read state → LLM → act → verify |
| `agent/tools.py` | מימוש 11 ה-Tools, dry-run, approval, retry/recovery |
| `agent/llm.py` | הפשטה לספקי LLM (OpenAI / Anthropic / DeepSeek) |
| `agent/prompts.py` | תבניות הפרומפט למודל |
| `agent/memory.py` | זיכרון המשימה: יעד, היסטוריה, שגיאות |
| `agent/logger.py` | הגדרת logging + רישום פעולות מובנה |
| `agent/utils.py` | `ActionResult`, `Timer`, `retry`, `extract_json` |
| `agent/actions/*.py` | הפעולות הבסיסיות מול Playwright (click/fill/read/navigate/wait) |

## דרישות מוקדמות

* Python 3.12 ומעלה.
* Google Chrome מותקן.
* מפתח API לספק ה-LLM שנבחר (OpenAI, Anthropic או DeepSeek).

## התקנה

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium      # מוריד את הדפדפנים הדרושים ל-Playwright עצמו
```

> שימו לב: הסוכן עצמו **לא** פותח דפדפן חדש - הוא מתחבר לדפדפן Chrome קיים
> (ראו בהמשך). `playwright install` נדרש רק כדי ש-Playwright יוכל לתקשר עם
> הדפדפן ברמת הפרוטוקול.

## הגדרת קונפיגורציה

```bash
cp config.yaml.example config.yaml
```

ערכו את `config.yaml`:

```yaml
target_url: "https://internal.example.local"
remote_debug_port: 9222
llm_provider: "openai"          # openai | anthropic | deepseek
model_name: "gpt-4o-mini"       # לדוגמה: "deepseek-chat" עבור deepseek
approval_mode: "none"           # none | each_action | final_only
dry_run: false
```

מפתח ה-API מומלץ להעביר דרך משתנה סביבה, לא לשמור בקובץ:

```bash
export OPENAI_API_KEY="sk-..."
# או
export ANTHROPIC_API_KEY="sk-ant-..."
# או
export DEEPSEEK_API_KEY="sk-..."
```

> **DeepSeek**: ה-API של DeepSeek תואם ל-OpenAI (`https://api.deepseek.com`),
> ולכן משתמש באותה ספריית `openai` שכבר מותקנת - אין צורך בחבילה נוספת.
> הגדירו `llm_provider: "deepseek"` ו-`model_name: "deepseek-chat"` (או
> `deepseek-reasoner`), וקבלו מפתח API בכתובת https://platform.deepseek.com.

## הפעלת Chrome עם Remote Debugging

הסוכן מתחבר לדפדפן **קיים** ואינו פותח חלון חדש. הריצו את Chrome עם דגל
remote debugging (מומלץ עם פרופיל נפרד, כדי לא להתנגש בדפדפן היומיומי שלכם):

```bash
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-agent-profile"

# Linux
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-agent-profile"

# Windows (PowerShell)
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 --user-data-dir="$env:USERPROFILE\chrome-agent-profile"
```

התחברו/נווטו ידנית לאתר הפנימי בטאב אחד, ואז הריצו את הסוכן - הוא יאתר את
הטאב לפי `target_url`, ואם לא נמצא טאב מתאים ייפתח טאב חדש **בתוך אותו
דפדפן קיים** (לא ייפתח תהליך דפדפן חדש).

## הרצה

```bash
python -m agent.main --goal "מלא את שם המשתמש בטופס ולחץ שלח"
```

### דוגמאות שימוש

```bash
# הרצה רגילה מול config.yaml
python -m agent.main --goal "חפש את המוצר 'מקלדת' והוסף אותו לעגלה"

# מצב Dry Run - הסוכן רק מדווח מה היה עושה, לא לוחץ ולא מקליד
python -m agent.main --goal "מחק את המשתמש X" --dry-run

# אישור ידני לפני כל פעולה
python -m agent.main --goal "עדכן את הכתובת בפרופיל" --approval-mode each_action

# אישור ידני רק לפני הפעולה הסופית (finished=true)
python -m agent.main --goal "שלח את הטופס" --approval-mode final_only

# קובץ קונפיגורציה חלופי
python -m agent.main --config config.staging.yaml --goal "..."
```

הרצה מסתיימת עם קוד יציאה `0` אם המשימה הושלמה, `1` אם לא הושלמה
(לדוגמה: אזל תקציב הצעדים או נדרש קלט אנושי), ו-`2` אם לא ניתן היה
להתחבר ל-Chrome.

## מצבי בטיחות

* **Dry Run** (`dry_run: true`) - פעולות משנות-מצב (click, fill, press,
  navigate, upload, download, refresh, scroll) רק נרשמות ומדווחות, לא
  מבוצעות בפועל. פעולות קריאה (`read`, `wait`, `screenshot`) עדיין רצות
  כדי לתת למודל הקשר אמיתי.
* **Approval Mode**:
  * `none` - ריצה אוטונומית מלאה.
  * `each_action` - אישור ידני (`y`/`N` בטרמינל) לפני **כל** פעולה.
  * `final_only` - אישור ידני נדרש רק לפני הפעולה שבה המודל מסמן
    `finished: true` (כלומר הצעד המסיים את המשימה).
* אם המודל אינו בטוח או שחסר מידע, הוא מחזיר `action: "ask"` והסוכן
  עוצר ומדווח שנדרשת החלטה אנושית, במקום לנחש.
* המודל מחויב להשתמש רק בסלקטורים שמופיעים בפועל במצב הדף שסופק לו -
  לעולם לא "ממציא" סלקטור.

## Error Recovery

לכל פעולה: ניסיון → אם נכשל, ניסיון חוזר (`retry_count` פעמים) → אם
עדיין נכשל, רענון הדף וניסיון נוסף → אם עדיין נכשל, צילום מסך ורישום
שגיאה מובנית ל-log ול-memory, והחזרת תוצאה ברורה ללולאה הראשית (שמעבירה
את הכישלון בחזרה למודל בצעד הבא, כדי שינסה גישה חלופית).

## כלי הפעולה (Tools)

| Tool | תיאור |
|---|---|
| `click(selector)` | לחיצה על אלמנט לפי סלקטור |
| `fill(selector, text)` | מילוי שדה טקסט |
| `read(selector)` | קריאת תוכן/ערך של אלמנט |
| `wait(selector)` | המתנה לאלמנט (visible/attached/...) |
| `screenshot()` | צילום מסך מלא או אלמנט בודד, נשמר ב-`agent/screenshots` |
| `navigate(url)` | ניווט לכתובת |
| `scroll()` | גלילה לאלמנט או גלילת עמוד |
| `press(key)` | לחיצת מקלדת |
| `upload(file)` | העלאת קובץ ל-`<input type=file>` |
| `download()` | לחיצה שמפעילה הורדה, ושמירתה ב-`agent/data` |
| `refresh()` | רענון הדף |

## בדיקות

```bash
# בדיקות יחידה בלבד (ללא דפדפן אמיתי)
python -m pytest tests -q --ignore=tests/integration

# כולל בדיקות אינטגרציה (מפעילות Chromium headless אמיתי)
playwright install chromium
python -m pytest tests -q
```

* `tests/test_utils.py`, `test_memory.py`, `test_actions.py`,
  `test_tools.py`, `test_llm.py` - בדיקות יחידה עם Page/Locator מדומים.
* `tests/integration/test_agent_flow.py` - בדיקת אינטגרציה מלאה: מפעילה
  Chromium headless אמיתי, מריצה את `AgentController` מול דף HTML מקומי
  עם LLM מדומה (`ScriptedLLM`), ומוודאת שה-DOM אכן השתנה כצפוי.

## אבטחה

* הסוכן פועל אך ורק מול `target_url` שהוגדר, ומתחבר לדפדפן קיים בלבד -
  לעולם לא פותח דפדפן/פרופיל חדש.
* לחיצות מתבצעות תמיד לפי סלקטור ב-DOM, לא לפי קואורדינטות עכבר.
* קריאת תוכן העמוד מתבצעת דרך ה-DOM (`innerText`/`value`), ללא OCR.
* המודל אינו מבצע פעולה שלא התבקשה, ועוצר ומבקש החלטה אנושית כשחסר מידע.
* מפתחות API נטענים ממשתני סביבה ולא נשמרים בברירת מחדל בקובץ קונפיגורציה
  (`config.yaml` נמצא ב-`.gitignore`).

## מבנה הפרויקט

```
Agent/
├── agent/
│   ├── main.py            # CLI entry point
│   ├── config.py          # AgentConfig + טעינת YAML
│   ├── browser.py         # חיבור CDP לדפדפן קיים
│   ├── controller.py      # לולאת ה-Agent הראשית
│   ├── tools.py           # ToolExecutor - 11 ה-Tools
│   ├── llm.py             # ספקי LLM (OpenAI/Anthropic/DeepSeek)
│   ├── prompts.py         # תבניות פרומפט
│   ├── memory.py          # זיכרון משימה
│   ├── logger.py          # הגדרת logging
│   ├── utils.py           # ActionResult, retry, extract_json
│   ├── actions/
│   │   ├── click.py
│   │   ├── fill.py
│   │   ├── read.py
│   │   ├── navigate.py
│   │   └── wait.py
│   ├── screenshots/        # צילומי מסך (נוצר אוטומטית)
│   ├── logs/                # קבצי log (נוצר אוטומטית)
│   └── data/                 # memory.json + קבצים שהורדו
├── tests/
│   ├── test_*.py            # בדיקות יחידה
│   └── integration/
│       └── test_agent_flow.py
├── config.yaml.example
├── requirements.txt
└── README.md
```
