import type { Question } from "../types";
import { formatTime } from "./Timeline";

const KIND_LABEL: Record<string, string> = {
  "take-choice": "Ripetizione",
  "pause-intent": "Pausa",
  "filler-inside": "Intercalare",
  boundary: "Punto di taglio",
  "low-confidence": "Trascrizione incerta",
};

interface Props {
  questions: Question[];
  selected: Question | null;
  busy: string | null;
  onSelect: (question: Question) => void;
  onAnswer: (question: Question, optionId: string) => void;
}

/** The heart of the screen. Everything the engine refused to decide shows up
 *  here, and the plan stays unapplicable until this list is empty. */
export function Questions({ questions, selected, busy, onSelect, onAnswer }: Props) {
  const open = questions.filter((question) => !question.resolved);
  const answered = questions.filter((question) => question.resolved);

  return (
    <aside className="questions">
      <header>
        <p className="eyebrow">DA DECIDERE</p>
        <h2>
          {open.length === 0 ? "Nessuna ambiguità" : `${open.length} ${open.length === 1 ? "domanda" : "domande"}`}
        </h2>
        <p className="hint">
          {open.length === 0
            ? "Il montaggio si può applicare."
            : "Il montaggio non viene applicato finché restano decisioni aperte."}
        </p>
      </header>

      <div className="question-list">
        {open.map((question) => (
          <Card
            key={question.id}
            question={question}
            selected={selected?.id === question.id}
            busy={busy === question.id}
            onSelect={() => onSelect(question)}
            onAnswer={(option) => onAnswer(question, option)}
          />
        ))}

        {answered.length > 0 && (
          <details className="answered">
            <summary>{answered.length} già decise</summary>
            {answered.map((question) => (
              <div key={question.id} className="answered-row" onClick={() => onSelect(question)}>
                <b>{formatTime(question.at)}</b>
                <span>{question.options.find((option) => option.id === question.answer)?.label ?? question.answer}</span>
              </div>
            ))}
          </details>
        )}
      </div>
    </aside>
  );
}

function Card({
  question,
  selected,
  busy,
  onSelect,
  onAnswer,
}: {
  question: Question;
  selected: boolean;
  busy: boolean;
  onSelect: () => void;
  onAnswer: (optionId: string) => void;
}) {
  return (
    <article className={selected ? "question selected" : "question"} onClick={onSelect}>
      <div className="question-head">
        <span className="kind">{KIND_LABEL[question.kind] ?? question.kind}</span>
        <span className="at">{formatTime(question.at)}</span>
      </div>
      <h3>{question.prompt}</h3>
      <p className="context">{question.context}</p>
      <div className="options">
        {question.options.map((option) => (
          <button
            key={option.id}
            className={option.recommended ? "option recommended" : "option"}
            disabled={busy}
            onClick={(event) => {
              event.stopPropagation();
              onAnswer(option.id);
            }}
          >
            <b>{option.label}</b>
            {option.detail && <small>{option.detail}</small>}
            {option.recommended && <i className="badge">consigliata</i>}
          </button>
        ))}
      </div>
    </article>
  );
}
