import type { Option, Question } from "../types";
import { formatTime } from "./Timeline";

const KIND_LABEL: Record<string, string> = {
  "take-choice": "Ripetizione",
  "pause-intent": "Pausa",
  "filler-inside": "Intercalare",
  boundary: "Punto di taglio",
  "low-confidence": "Trascrizione incerta",
};

/** What `answeredBy` says when nobody was asked. */
export const ENGINE = "motore";

const decidedByEngine = (question: Question) =>
  question.resolved && question.answeredBy === ENGINE;

/** The order the screen walks you through: first anything the engine would not
 *  decide, then everything it decided on your behalf.
 *
 *  The second half is the point. The engine no longer stops to ask, so without
 *  this the choices it made would be invisible — the screen would say "nessuna
 *  ambiguità" over an edit full of them. A decision you cannot see is not a
 *  decision you made. */
export function reviewQueue(questions: Question[]): Question[] {
  return [
    ...questions.filter((question) => !question.resolved),
    ...questions.filter(decidedByEngine),
  ];
}

interface Props {
  questions: Question[];
  selected: Question | null;
  busy: string | null;
  playingOption: string | null;
  onSelect: (question: Question) => void;
  onAnswer: (question: Question, optionId: string) => void;
  onListen: (question: Question, option: Option) => void;
}

/** The heart of the screen: every judgement call in the edit, one at a time.
 *
 *  One at a time on purpose — six of them stacked made a page two screens tall,
 *  which meant scrolling away from the video you need in order to answer. */
export function Questions({
  questions,
  selected,
  busy,
  playingOption,
  onSelect,
  onAnswer,
  onListen,
}: Props) {
  const queue = reviewQueue(questions);
  const confirmed = questions.filter((question) => question.resolved && !decidedByEngine(question));
  const current = queue.find((question) => question.id === selected?.id) ?? queue[0] ?? null;
  const position = current ? queue.findIndex((question) => question.id === current.id) : -1;

  if (!current) {
    return (
      <aside className="decide decide-clear">
        <p className="eyebrow">DECISIONI</p>
        <h2>Tutto rivisto</h2>
        <p className="hint">
          {confirmed.length > 0
            ? `${confirmed.length} confermate a mano. Il montaggio si può applicare.`
            : "Nessun caso dubbio in questo girato: il montaggio si può applicare."}
        </p>
        {confirmed.length > 0 && <Answered questions={confirmed} onSelect={onSelect} />}
      </aside>
    );
  }

  const reviewing = decidedByEngine(current);

  return (
    <aside className={reviewing ? "decide decide-review" : "decide"}>
      <header className="decide-head">
        <div>
          <p className="eyebrow">
            {reviewing ? "DECISE DAL MOTORE" : "DA DECIDERE"} · {position + 1} di {queue.length}
          </p>
          <h2>{current.prompt}</h2>
        </div>
        <nav className="decide-nav">
          <button
            className="ghost small"
            disabled={position <= 0}
            onClick={() => onSelect(queue[position - 1])}
            aria-label="Domanda precedente"
          >
            ←
          </button>
          <button
            className="ghost small"
            disabled={position < 0 || position >= queue.length - 1}
            onClick={() => onSelect(queue[position + 1])}
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
      {reviewing && (
        <p className="hint">
          Il motore ha già scelto e il montaggio ne tiene conto. Ascolta e conferma, oppure
          cambia: la proposta si rifà attorno alla tua scelta.
        </p>
      )}

      <div className="options">
        {current.options.map((option) => {
          const taken = reviewing && option.id === current.answer;
          const classes = ["option"];
          if (taken) classes.push("taken");
          else if (option.recommended && !reviewing) classes.push("recommended");
          return (
            <div key={option.id} className={classes.join(" ")}>
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
                  {busy === current.id ? "…" : taken ? "Confermo" : "Scegli"}
                </button>
              </div>
              {taken && <i className="badge">scelta dal motore</i>}
              {!taken && option.recommended && !reviewing && <i className="badge">consigliata</i>}
            </div>
          );
        })}
      </div>

      {confirmed.length > 0 && <Answered questions={confirmed} onSelect={onSelect} />}
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
      <summary>{questions.length} confermate a mano</summary>
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
