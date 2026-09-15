import type { Option, Question } from "../types";
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
  playingOption: string | null;
  onSelect: (question: Question) => void;
  onAnswer: (question: Question, optionId: string) => void;
  onListen: (question: Question, option: Option) => void;
}

/** The heart of the screen. Everything the engine refused to decide comes here,
 *  and the plan stays unapplicable until none is left.
 *
 *  One question at a time, on purpose: six of them stacked made a page two
 *  screens tall, which meant scrolling away from the video you need in order to
 *  answer. Deciding is the work; everything else is context. */
export function Questions({
  questions,
  selected,
  busy,
  playingOption,
  onSelect,
  onAnswer,
  onListen,
}: Props) {
  const open = questions.filter((question) => !question.resolved);
  const answered = questions.filter((question) => question.resolved);
  const current = selected && !selected.resolved ? selected : open[0] ?? null;
  const position = current ? open.findIndex((question) => question.id === current.id) : -1;

  if (!current) {
    return (
      <aside className="decide decide-clear">
        <p className="eyebrow">DA DECIDERE</p>
        <h2>Nessuna ambiguità</h2>
        <p className="hint">
          {answered.length > 0
            ? `${answered.length} decise. Il montaggio si può applicare.`
            : "Il motore non ha trovato casi dubbi: il montaggio si può applicare."}
        </p>
        {answered.length > 0 && <Answered questions={answered} onSelect={onSelect} />}
      </aside>
    );
  }

  return (
    <aside className="decide">
      <header className="decide-head">
        <div>
          <p className="eyebrow">
            DA DECIDERE · {position + 1} di {open.length}
          </p>
          <h2>{current.prompt}</h2>
        </div>
        <nav className="decide-nav">
          <button
            className="ghost small"
            disabled={position <= 0}
            onClick={() => onSelect(open[position - 1])}
            aria-label="Domanda precedente"
          >
            ←
          </button>
          <button
            className="ghost small"
            disabled={position < 0 || position >= open.length - 1}
            onClick={() => onSelect(open[position + 1])}
            aria-label="Domanda successiva"
          >
            →
          </button>
        </nav>
      </header>

      <p className="decide-meta">
        <span className="kind">{KIND_LABEL[current.kind] ?? current.kind}</span>
        <span className="at">a {formatTime(current.at)}</span>
      </p>
      <p className="context">{current.context}</p>

      <div className="options">
        {current.options.map((option) => (
          <div
            key={option.id}
            className={option.recommended ? "option recommended" : "option"}
          >
            <div className="option-text">
              <b>{option.label}</b>
              {option.detail && <small>{option.detail}</small>}
            </div>
            <div className="option-actions">
              {option.start !== undefined && option.end !== undefined && (
                <button
                  className="listen"
                  onClick={() => onListen(current, option)}
                  aria-label={`Ascolta: ${option.label}`}
                >
                  {playingOption === option.id ? "◼ ferma" : "▶ ascolta"}
                </button>
              )}
              <button
                className="choose"
                disabled={busy === current.id}
                onClick={() => onAnswer(current, option.id)}
              >
                {busy === current.id ? "…" : "Scegli"}
              </button>
            </div>
            {option.recommended && <i className="badge">consigliata</i>}
          </div>
        ))}
      </div>

      {answered.length > 0 && <Answered questions={answered} onSelect={onSelect} />}
    </aside>
  );
}

function Answered({
  questions,
  onSelect,
}: {
  questions: Question[];
  onSelect: (question: Question) => void;
}) {
  return (
    <details className="answered">
      <summary>{questions.length} già decise</summary>
      {questions.map((question) => (
        <button key={question.id} className="answered-row" onClick={() => onSelect(question)}>
          <b>{formatTime(question.at)}</b>
          <span>
            {question.options.find((option) => option.id === question.answer)?.label ??
              question.answer}
          </span>
        </button>
      ))}
    </details>
  );
}
