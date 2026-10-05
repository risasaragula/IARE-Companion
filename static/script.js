const modal = document.getElementById("askModal");
const input = document.getElementById("questionInput");
const answer = document.getElementById("answerArea");

let busy = false;
let opener = null;


function scrollToFeatures() {
    const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    document.getElementById("features").scrollIntoView({
        behavior: smooth ? "smooth" : "auto"
    });
}


function openAsk() {
    opener = document.activeElement;
    modal.classList.add("show");
    modal.setAttribute("aria-hidden", "false");
    setTimeout(() => input.focus(), 100);
}


function closeAsk() {
    modal.classList.remove("show");
    modal.setAttribute("aria-hidden", "true");
    if (opener) opener.focus();
}


function fillQuestion(question) {
    input.value = question;
    input.focus();
}


function setAnswer(text) {
    answer.textContent = "";

    const icon = document.createElement("span");
    icon.textContent = "✦";

    answer.style.whiteSpace = "pre-wrap";
    answer.append(icon, " ", text);
}


async function askQuestion() {
    if (busy) return;

    const question = input.value.trim();

    if (!question) {
        setAnswer("Please type a question first.");
        return;
    }

    // Attendance saved by the Attendance page (if the student analyzed one)
    let attendance = null;
    try {
        attendance = JSON.parse(localStorage.getItem("iare_attendance"));
    } catch (e) {
        attendance = null;
    }

    busy = true;
    setAnswer("Thinking...");

    try {
        const response = await fetch("/ask", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question, attendance })
        });

        const data = await response.json();

        setAnswer(
            data.success
                ? data.answer
                : (data.message || "Something went wrong. Please try again.")
        );
    } catch (error) {
        console.error(error);
        setAnswer("Couldn't reach the server. Make sure it is running and try again.");
    } finally {
        busy = false;
    }
}


input.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
        event.preventDefault();
        askQuestion();
    }
});


modal.addEventListener("click", function (event) {
    if (event.target === modal) closeAsk();
});


document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && modal.classList.contains("show")) {
        closeAsk();
    }
});


// Hero card: show the student's real attendance if they analyzed a screenshot
function updateHero() {
    const big = document.getElementById("heroOverall");
    if (!big) return;                       // not on the home page

    let data = null;
    try {
        data = JSON.parse(localStorage.getItem("iare_attendance"));
    } catch (e) {
        return;
    }

    if (!data || data.overall == null || !Array.isArray(data.subjects)) return;

    const subs = data.subjects.filter(s => s.conducted > 0);
    if (!subs.length) return;

    const overall = Number(data.overall);
    const [whole, decimal] = overall.toFixed(1).split(".");

    big.textContent = whole;
    const small = document.createElement("span");
    small.textContent = "." + decimal + "%";
    big.appendChild(small);

    document.getElementById("heroFill").style.width = Math.min(overall, 100) + "%";

    // three lowest subjects
    const lowest = [...subs].sort((a, b) => a.percentage - b.percentage).slice(0, 3);
    const row = document.getElementById("heroSubjects");
    row.textContent = "";

    lowest.forEach(s => {
        const box = document.createElement("div");
        if (s.percentage < 75) box.className = "danger";

        const label = document.createElement("span");
        label.textContent = s.code;

        const value = document.createElement("strong");
        value.textContent = Math.round(s.percentage) + "%";

        box.append(label, value);
        row.appendChild(box);
    });

    // warning line about the weakest subject
    const worst = lowest[0];
    const title = document.getElementById("heroWarnTitle");
    const text = document.getElementById("heroWarnText");

    if (worst.percentage < 75) {
        const need = Math.max(0, 3 * worst.conducted - 4 * worst.attended);
        title.textContent = worst.name + " needs attention";
        text.textContent = "Attend the next " + need + (need === 1 ? " class" : " classes") + " in a row to reach 75%";
    } else {
        title.textContent = "You're on track";
        text.textContent = "All subjects are at or above 75%";
    }
}

updateHero();
