import os
import re

import pytesseract
from flask import Flask, jsonify, render_template, request
from PIL import Image, ImageEnhance, ImageFilter

app = Flask(__name__)

TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.path.exists(TESSERACT_PATH):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

# Ask IARE (AI). The key is read from an environment variable, never from the browser.
try:
    import anthropic
    ai_client = anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
except ImportError:
    ai_client = None

AI_MODEL = "claude-haiku-4-5-20251001"
AI_SYSTEM = (
    "You are Ask IARE, a friendly assistant inside IARE Companion, a web app for students of IARE "
    "(Institute of Aeronautical Engineering). Keep answers short, clear and practical. "
    "The app has four features: Attendance (upload a Samvidha screenshot), Calculators (SGPA, CGPA, "
    "percentage, marks needed), Discover (internships, hackathons, competitions, workshops, "
    "scholarships, placements) and Ask IARE. When a question fits one of them, point the student to it. "
    "Do not invent IARE-specific facts (rules, dates, fees, results, contacts) or specific opportunities "
    "and deadlines. If you are not sure, say so and suggest checking Samvidha, the official IARE website "
    "or the college office. For attendance numbers use only the data provided; never guess them."
)


# ---------------------------------------------------------
# ROUTES
# ---------------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/attendance")
def attendance():
    return render_template("attendance.html")


@app.route("/calculators")
def calculators():
    return render_template("calculators.html")


@app.route("/discover")
def discover():
    return render_template("discover.html")


# ---------------------------------------------------------
# IMAGE + OCR
# ---------------------------------------------------------

def prepare_image(image):
    image = image.convert("RGB")
    image = image.resize((image.width * 3, image.height * 3))
    gray = ImageEnhance.Contrast(image.convert("L")).enhance(2)
    return gray.filter(ImageFilter.SHARPEN)


def get_words(image):
    data = pytesseract.image_to_data(
        image, config="--psm 6", output_type=pytesseract.Output.DICT
    )
    words = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except ValueError:
            conf = 0
        if conf < 10:
            continue
        x, y = data["left"][i], data["top"][i]
        w, h = data["width"][i], data["height"][i]
        words.append({"text": text, "x": x, "y": y, "w": w, "h": h,
                      "cx": x + w / 2, "cy": y + h / 2})
    return words


def group_rows(words):
    rows = []
    for word in sorted(words, key=lambda w: w["cy"]):
        for row in rows:
            if abs(word["cy"] - row["cy"]) <= max(25, word["h"] * 1.5):
                row["words"].append(word)
                row["cy"] = sum(w["cy"] for w in row["words"]) / len(row["words"])
                break
        else:
            rows.append({"cy": word["cy"], "words": [word]})
    for row in rows:
        row["words"].sort(key=lambda w: w["x"])
    rows.sort(key=lambda r: r["cy"])
    return rows


# ---------------------------------------------------------
# TEXT HELPERS
# ---------------------------------------------------------

NUM = re.compile(r"\d+(?:\.\d+)?")
STOP = {"T", "L", "PW", "CORE", "PROJECT", "SKILL", "AUDIT", "MC",
        "PE-I", "PE-II", "PE-III", "PE-IV", "PE-V", "OE-I", "OE-II"}


def clean(text):
    return text.replace("—", "-").replace("–", "-").strip()


def to_number(text):
    """Lenient number parse for short tokens like 'l2' or 'O5'."""
    text = clean(text)
    if not re.fullmatch(r"[0-9OoIl|.]+", text):
        return None
    text = text.translate(str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"}))
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def is_course_code(text):
    return bool(re.fullmatch(r"[A-Z]{3,6}\d{2,3}", text.upper().strip()))


def as_int(value):
    if value is not None and float(value).is_integer():
        return int(value)
    return None


# ---------------------------------------------------------
# HEADER + ROW PARSING
# ---------------------------------------------------------

def find_header(rows):
    best, best_score = None, 0
    keywords = (("course", 1), ("code", 1), ("conducted", 2),
                ("attended", 2), ("attendance", 1))
    for row in rows:
        text = " ".join(w["text"].lower() for w in row["words"])
        score = sum(pts for key, pts in keywords if key in text)
        if score > best_score:
            best, best_score = row, score

    if not best or best_score < 3:
        return None

    cols = {}
    for w in best["words"]:
        t = w["text"].lower().strip()
        if "conduct" in t:
            cols.setdefault("conducted", w["cx"])
        elif "attendance" in t or "%" in t:      # must come BEFORE "attend"
            cols.setdefault("percentage", w["cx"])
        elif "attend" in t:
            cols.setdefault("attended", w["cx"])

    print("HEADER COLUMNS:", cols)
    return {"row": best, "columns": cols}


def nearest_int(nums, x, tol):
    best = None
    for cx, value in nums:
        distance = abs(cx - x)
        if distance <= tol and (best is None or distance < best[0]):
            best = (distance, value)
    return as_int(best[1]) if best else None


def read_row(words, cols, tol):
    idx = next((i for i, w in enumerate(words) if is_course_code(clean(w["text"]))), None)
    if idx is None:
        return None

    code = clean(words[idx]["text"]).upper()

    serial = None
    for w in words[:idx]:
        n = to_number(w["text"])
        if isinstance(n, int) and 1 <= n <= 100:
            serial = n
            break

    # Course name = words after the code, up to the first number / table field
    name, j = [], idx + 1
    while j < len(words):
        t = clean(words[j]["text"])
        if NUM.fullmatch(t) or (name and t.upper() in STOP):
            break
        name.append(t)
        j += 1

    nums = [(w["cx"], float(clean(w["text"])))
            for w in words[j:] if NUM.fullmatch(clean(w["text"]))]

    conducted = attended = None

    # 1) Self-validating: find consecutive (conducted, attended, percentage)
    for i in range(len(nums) - 2):
        c, a, p = as_int(nums[i][1]), as_int(nums[i + 1][1]), nums[i + 2][1]
        if c is None or a is None or c <= 0 or a > c:
            continue
        if abs(a / c * 100 - p) <= 1.5:
            conducted, attended = c, a
            break

    # 2) Fallback: use header column positions
    if conducted is None and "conducted" in cols and "attended" in cols:
        c = nearest_int(nums, cols["conducted"], tol)
        a = nearest_int(nums, cols["attended"], tol)
        if c is not None and a is not None and 0 <= a <= c:
            conducted, attended = c, a

    if conducted is None:
        return None

    return {"serial": serial, "code": code,
            "name": " ".join(name) or code,
            "conducted": conducted, "attended": attended}


def add_stats(s):
    c, a = s["conducted"], s["attended"]
    s["percentage"] = round(a / c * 100, 2) if c else 0
    # classes needed in a row to reach 75%, or classes that can still be missed
    s["need"] = max(0, 3 * c - 4 * a) if c else 0
    s["can_miss"] = (4 * a - 3 * c) // 3 if c and 4 * a >= 3 * c else 0
    return s


def extract_attendance(image):
    rows = group_rows(get_words(prepare_image(image)))

    print("\n======== OCR OUTPUT ========")
    for row in rows:
        print(" | ".join(w["text"] for w in row["words"]))
    print("============================")

    header = find_header(rows)
    cols = header["columns"] if header else {}
    header_cy = header["row"]["cy"] if header else -1
    tol = max(60, image.width * 3 * 0.035)

    subjects, seen = [], set()
    for row in rows:
        if row["cy"] <= header_cy:
            continue
        subject = read_row(row["words"], cols, tol)
        if not subject or subject["code"] in seen:
            continue
        seen.add(subject["code"])
        if subject["serial"] is None:
            subject["serial"] = len(subjects) + 1
        print("FOUND:", subject)
        subjects.append(add_stats(subject))

    subjects.sort(key=lambda s: s["serial"])
    return subjects

@app.route("/placement")
def placement():
    return render_template("placement.html")

# ---------------------------------------------------------
# UPLOAD
# ---------------------------------------------------------

@app.route("/upload-attendance", methods=["POST"])
def upload_attendance():
    file = request.files.get("attendance_image")
    if not file or file.filename == "":
        return jsonify({"success": False, "message": "Please upload an attendance screenshot."})

    try:
        subjects = extract_attendance(Image.open(file.stream))
    except pytesseract.TesseractNotFoundError:
        return jsonify({"success": False,
                        "message": "Tesseract OCR is not installed or its path is wrong in app.py."})
    except Exception as e:
        print("OCR ERROR:", e)
        return jsonify({"success": False, "message": f"Could not read the image: {e}"})

    if not subjects:
        return jsonify({"success": False,
                        "message": "Couldn't read the attendance table. Use a clear screenshot "
                                   "that includes the header row (Course Code, Conducted, Attended)."})

    valid = [s for s in subjects if s["conducted"] > 0]
    total_attended = sum(s["attended"] for s in valid)
    total_conducted = sum(s["conducted"] for s in valid)
    overall = round(total_attended / total_conducted * 100, 2) if total_conducted else None

    risk = sorted((s for s in valid if s["percentage"] < 75), key=lambda s: s["percentage"])

    priority = []
    for s in valid:
        current = s["attended"] / s["conducted"] * 100
        after_one = (s["attended"] + 1) / (s["conducted"] + 1) * 100
        priority.append({**s,
                         "after_one": round(after_one, 2),
                         "improvement": round(after_one - current, 2),
                         "after_miss": round(s["attended"] / (s["conducted"] + 1) * 100, 2)})
    priority.sort(key=lambda s: s["improvement"], reverse=True)

    return jsonify({"success": True, "overall": overall,
                    "total_attended": total_attended,
                    "total_conducted": total_conducted,
                    "subjects": subjects, "risk": risk, "priority": priority[:3]})


# ---------------------------------------------------------
# ASK IARE
# ---------------------------------------------------------

def clean_subjects(att):
    """Validate attendance data sent by the browser."""
    if not isinstance(att, dict) or not isinstance(att.get("subjects"), list):
        return []
    out = []
    for s in att["subjects"][:30]:
        try:
            c, a = int(s["conducted"]), int(s["attended"])
            name = str(s.get("name") or s.get("code") or "Subject")[:60]
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if c > 0 and 0 <= a <= c:
            out.append({"name": name, "attended": a, "conducted": c})
    return out


def plural(n):
    return f"{n} class" + ("" if n == 1 else "es")


def tip_for(a, c):
    if 4 * a >= 3 * c:
        can_miss = (4 * a - 3 * c) // 3
        if can_miss:
            return f"you can miss {plural(can_miss)} and stay at 75%"
        return "you're right at 75%, don't miss the next class"
    return f"attend the next {plural(3 * c - 4 * a)} in a row to reach 75%"


ATTENDANCE_WORDS = ("miss", "bunk", "skip", "leave", "75", "need to attend", "how many classes")


def attendance_answer(question, subjects):
    q = question.lower()
    if not any(w in q for w in ATTENDANCE_WORDS):
        return None
    if not subjects:
        return ("Upload your attendance screenshot on the Attendance page first, "
                "then ask me again and I'll calculate it from your real numbers.")
    total_a = sum(s["attended"] for s in subjects)
    total_c = sum(s["conducted"] for s in subjects)
    lines = [f"Overall: {total_a / total_c * 100:.2f}% ({total_a}/{total_c}), {tip_for(total_a, total_c)}.", ""]
    for s in subjects:
        a, c = s["attended"], s["conducted"]
        lines.append(f"- {s['name']}: {a / c * 100:.2f}% ({a}/{c}), {tip_for(a, c)}")
    return "\n".join(lines)


FAQ = [
    (("sgpa",),
     "SGPA = sum of (credits x grade point) for all subjects, divided by total credits.\n\n"
     "Example: 4 credits at 9 and 3 credits at 8 gives (36 + 24) / 7 = 8.57.\n\n"
     "Use the Calculators page to do it for all your subjects."),
    (("cgpa",),
     "CGPA = sum of (semester credits x semester SGPA), divided by total credits across semesters.\n\n"
     "Use the CGPA calculator on the Calculators page. Converting CGPA to a percentage follows your "
     "college's own rule, so check that with your department."),
    (("percentage", "percent", "marks obtained"),
     "Percentage = marks obtained / maximum marks x 100.\n\n"
     "The Percentage Calculator on the Calculators page does this for you."),
    (("marks needed", "target", "how many marks", "need to score", "pass"),
     "Marks needed = (target % / 100) x (current maximum + upcoming maximum) - current marks.\n\n"
     "The Marks Needed calculator on the Calculators page does this and tells you if the target is reachable."),
    (("calculator", "calculate"),
     "The Calculators page has four tools: SGPA, CGPA, Percentage and Marks Needed. "
     "Ask me how any of them works, or open the page from the home screen."),
    (("opportunit", "internship", "hackathon", "competition", "workshop", "scholarship", "placement"),
     "Open the Discover page from the home screen. It has Internships, Hackathons, Competitions, "
     "Workshops, Scholarships and Placements. For deadlines and eligibility, always confirm on the "
     "official page of each opportunity."),
    (("upload", "screenshot", "samvidha", "how to use", "how do i use"),
     "On the Attendance page, upload a clear Samvidha attendance screenshot that includes the table "
     "header (Course Code, Conducted, Attended). You get your overall attendance, subjects at risk, "
     "what to attend first, and a What-if calculator."),
    (("hello", "hi ", "hey", "help", "what can you do"),
     "I can help with:\n"
     "- Attendance: ask \"how many classes can I miss?\" after uploading your screenshot\n"
     "- SGPA, CGPA, percentage and marks-needed formulas\n"
     "- Finding opportunities on the Discover page"),
]


def faq_answer(question):
    q = " " + question.lower() + " "
    best, best_score = None, 0
    for triggers, answer in FAQ:
        score = sum(1 for t in triggers if t in q)
        if score > best_score:
            best, best_score = answer, score
    return best


NO_MATCH = ("I can help with attendance (like \"how many classes can I miss?\"), SGPA, CGPA, "
            "percentage and marks-needed formulas, and finding opportunities on the Discover page. "
            "Try asking one of those.")


@app.route("/ask", methods=["POST"])
def ask():
    body = request.get_json(silent=True) or {}
    question = str(body.get("question", "")).strip()[:500]
    if not question:
        return jsonify({"success": False, "message": "Type a question first."})

    subjects = clean_subjects(body.get("attendance"))

    quick = attendance_answer(question, subjects)
    if quick:
        return jsonify({"success": True, "answer": quick})

    known = faq_answer(question)
    if known:
        return jsonify({"success": True, "answer": known})

    if ai_client is None:
        return jsonify({"success": True, "answer": NO_MATCH})

    context = ""
    if subjects:
        rows = "; ".join(f"{s['name']} {s['attended']}/{s['conducted']}" for s in subjects)
        context = f"Student's attendance (attended/conducted): {rows}\n\n"

    try:
        reply = ai_client.messages.create(
            model=AI_MODEL,
            max_tokens=500,
            system=AI_SYSTEM,
            messages=[{"role": "user", "content": context + question}],
        )
        answer = "".join(b.text for b in reply.content if b.type == "text").strip()
    except Exception as e:
        print("AI ERROR:", e)
        return jsonify({"success": False, "message": "Ask IARE couldn't answer right now. Try again in a moment."})

    return jsonify({"success": True, "answer": answer or "I couldn't come up with an answer. Try rephrasing."})

# =========================================================
# PASTE INTO app.py, ABOVE the line:  if __name__ == "__main__":
# (If you pasted an earlier version, delete that block first.)
# FREE: uses the same Tesseract OCR as the Attendance page.
# =========================================================

# ---------------------------------------------------------
# CALCULATORS: READ GRADES FROM A RESULTS SCREENSHOT
# ---------------------------------------------------------

GRADES = {"S", "O", "A+", "A", "B+", "B", "C", "D", "F", "AB"}
GRADE_FOR_POINT = {10: "S", 9: "A+", 8: "A", 7: "B+", 6: "B", 5: "C", 0: "F"}
POINT_FOR_GRADE = {"S": 10, "O": 10, "A+": 9, "A": 8, "B+": 7, "B": 6, "C": 5, "F": 0}


def norm_grade(text):
    """Fix common OCR slips such as B+ read as 'Bt' or 'B*'."""
    t = text.upper().strip()
    if len(t) == 2 and t[0] in "AB" and t[1] in "T*":
        t = t[0] + "+"
    return t


def lenient_code(text):
    """Return a clean course code (e.g. AHSD05) or None. Fixes O/0 style OCR slips."""
    t = clean(text).upper()
    if is_course_code(t):
        return t
    m = re.fullmatch(r"([A-Z]{4})([0-9OIL]{2})", t)
    if m and any(ch.isdigit() for ch in m.group(2)):
        tail = m.group(2).translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
        return m.group(1) + tail
    return None


def parse_grade_tokens(toks):
    """toks = words on one table line, left to right, starting at the course code.
    Pattern after the name: [grade] grade_point [status] credits [attendance]."""
    for k in range(1, len(toks)):
        t = toks[k].replace(",", ".")
        if not re.fullmatch(r"\d{1,2}", t) or not 0 <= int(t) <= 10:
            continue                                   # not a grade point

        credits = None
        for nxt in toks[k + 1:k + 3]:                  # skip an optional status like "P"
            nxt = nxt.replace(",", ".")
            if re.fullmatch(r"\d+\.\d+", nxt) and float(nxt) <= 10:
                credits = float(nxt)
                break
        if credits is None:
            continue

        point = int(t)
        prev = toks[k - 1].upper() if k >= 2 else ""
        grade = prev if prev in GRADES else GRADE_FOR_POINT.get(point, "")

        name_words = toks[1:k]
        if name_words and (name_words[-1].upper() in GRADES or len(name_words[-1]) <= 2):
            name_words = name_words[:-1]               # drop the grade letter from the name
        name = " ".join(name_words)

        return {"name": name, "credits": credits, "grade": grade,
                "grade_point": float(point), "marks_obtained": None, "max_marks": None}

    # Backup: the grade point was unreadable, so use the grade letter instead
    for k in range(1, len(toks)):
        grade = norm_grade(toks[k])
        if grade not in POINT_FOR_GRADE:
            continue
        for nxt in toks[k + 1:k + 4]:
            nxt = nxt.replace(",", ".")
            if re.fullmatch(r"\d+\.\d+", nxt) and float(nxt) <= 10:
                name_words = toks[1:k]
                return {"name": " ".join(name_words), "credits": float(nxt), "grade": grade,
                        "grade_point": float(POINT_FOR_GRADE[grade]),
                        "marks_obtained": None, "max_marks": None}
    return None


@app.route("/read-marks", methods=["POST"])
def read_marks():
    # The page sends the image file itself as the request body.
    # (A normal multipart upload with the field "marks_image" also works.)
    from io import BytesIO
    file = request.files.get("marks_image")
    raw = file.read() if file else request.get_data()
    if not raw:
        return jsonify({"success": False, "message": "Please upload a screenshot."})

    try:
        image = Image.open(BytesIO(raw))
        words = get_words(prepare_image(image))
        rows = group_rows(words)
    except pytesseract.TesseractNotFoundError:
        return jsonify({"success": False,
                        "message": "Tesseract OCR is not installed or its path is wrong in app.py."})
    except Exception as e:
        return jsonify({"success": False, "message": f"Could not read the image: {e}"})

    print("\n======== MARKS OCR OUTPUT ========")
    for row in rows:
        print(" | ".join(w["text"] for w in row["words"]))
    print("==================================")

    # Semester heading and the SGPA / CGPA printed on the sheet
    semester_name, reported_sgpa, reported_cgpa = "", None, None
    for row in rows:
        text = " ".join(w["text"] for w in row["words"]).upper()
        m = re.search(r"\b(VIII|VII|VI|IV|V|III|II|I)\s+SEMESTER\b", text)
        if m and not semester_name:
            semester_name = m.group(0)
        m = re.search(r"SGPA\W*(\d+\.\d+)", text)
        if m:
            reported_sgpa = float(m.group(1))
        m = re.search(r"CGPA\W*(\d+\.\d+)", text)
        if m:
            reported_cgpa = float(m.group(1))

    # Anchor on each course code, then collect the words on that code's line.
    codes = []
    for w in words:
        code = lenient_code(w["text"])
        if code:
            codes.append((w, code))
    codes.sort(key=lambda c: c[0]["cy"])

    gaps = [b[0]["cy"] - a[0]["cy"] for a, b in zip(codes, codes[1:])
            if b[0]["cy"] - a[0]["cy"] > 5]
    tol = 0.55 * min(gaps) if gaps else 45

    subjects, seen, skipped = [], set(), []
    for cw, code in codes:
        if code in seen:
            continue
        line = sorted((w for w in words
                       if w is not cw and abs(w["cy"] - cw["cy"]) <= tol and w["x"] >= cw["x"]),
                      key=lambda w: w["x"])
        toks = [code] + [clean(w["text"]) for w in line]
        subject = parse_grade_tokens(toks)
        if subject:
            subject["name"] = subject["name"] or code
            seen.add(code)
            subjects.append(subject)
            print("FOUND:", code, subject)
        else:
            print("COULD NOT PARSE:", code, toks)
            if code not in skipped:
                skipped.append(code)

    if not subjects:
        return jsonify({"success": False,
                        "message": "Couldn't read the results table. Use a clear, zoomed-in screenshot "
                                   "that includes the Grade, Grade Point and Credits columns."})

    return jsonify({"success": True, "subjects": subjects, "semesters": [],
                    "semester_name": semester_name,
                    "reported_sgpa": reported_sgpa,
                    "reported_cgpa": reported_cgpa,
                    "skipped": [c for c in skipped if c not in seen]})

if __name__ == "__main__":
    app.run(debug=True)