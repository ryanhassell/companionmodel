const input = document.getElementById("parent-chat-input");
const contextInput = document.getElementById("parent-chat-context");
const activeQuestion = document.getElementById("questions-active-question");
const activeQuestionLabel = document.getElementById("questions-active-question-label");
const activeQuestionHelper = document.getElementById("questions-active-question-helper");
const clearButton = document.getElementById("questions-clear-active");
const questionButtons = Array.from(document.querySelectorAll("[data-question-fill]"));
const selectedQuestionKey =
  document.body?.dataset.selectedQuestionKey ||
  document.documentElement?.dataset.selectedQuestionKey ||
  document.querySelector("[data-selected-question-key]")?.getAttribute("data-selected-question-key") ||
  "";

if (input && contextInput && activeQuestion && activeQuestionLabel && activeQuestionHelper && questionButtons.length) {
  const syncQuestionQuery = (nextKey) => {
    try {
      const url = new URL(window.location.href);
      if (nextKey) {
        url.searchParams.set("question", nextKey);
      } else {
        url.searchParams.delete("question");
      }
      window.history.replaceState({}, "", url.toString());
    } catch {
      // Ignore URL state sync failures.
    }
  };

  const setSelectedCard = (button) => {
    questionButtons.forEach((candidate) => {
      const card = candidate.closest(".guidance-question-card");
      if (card) {
        card.classList.toggle("is-selected", candidate === button);
      }
    });
  };

  const clearActiveQuestion = () => {
    contextInput.value = "";
    activeQuestion.hidden = true;
    activeQuestionLabel.textContent = "";
    activeQuestionHelper.textContent = "";
    input.placeholder = "Answer in your own words or ask a follow-up...";
    setSelectedCard(null);
    syncQuestionQuery("");
  };

  questionButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const question = button.dataset.question || "";
      const prompt = button.dataset.prompt || "";
      const questionKey = button.dataset.questionKey || "";
      contextInput.value = question;
      activeQuestion.hidden = false;
      activeQuestionLabel.textContent = question;
      activeQuestionHelper.textContent = prompt;
      input.placeholder = prompt || "Answer in your own words or ask a follow-up...";
      setSelectedCard(button);
      syncQuestionQuery(questionKey);
      input.focus();
      input.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  });

  clearButton?.addEventListener("click", clearActiveQuestion);

  if (selectedQuestionKey) {
    const matching = questionButtons.find((button) => button.dataset.questionKey === selectedQuestionKey);
    if (matching) {
      matching.click();
    }
  }
}
