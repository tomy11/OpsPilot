// Models sometimes answer in Markdown even when asked for plain text. This shows the common marks
// as formatting. It builds React elements only (never HTML), and anything it does not know stays
// visible as typed.

const inline = /(\*\*[^*\n]+\*\*|`[^`\n]+`)/;

function Inline({ text }) {
  return text.split(inline).map((part, index) => {
    if (part.length > 4 && part.startsWith("**") && part.endsWith("**"))
      return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.length > 2 && part.startsWith("`") && part.endsWith("`"))
      return (
        <code key={index} className="rounded bg-muted px-1 font-mono text-[0.9em]">
          {part.slice(1, -1)}
        </code>
      );
    return part;
  });
}

const cells = (line) =>
  line
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((cell) => cell.trim());
const isRow = (line) => /^\s*\|.*\|\s*$/.test(line ?? "");
const isRule = (line) => /^\s*\|[\s:|-]+\|\s*$/.test(line ?? "") && line.includes("-");

function Table({ rows }) {
  const [head, ...body] = rows;
  return (
    <div className="my-2 overflow-x-auto">
      <table className="text-sm">
        <thead>
          <tr>
            {head.map((cell, i) => (
              <th key={i} className="border-b px-3 py-1.5 text-left font-semibold">
                <Inline text={cell} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, r) => (
            <tr key={r}>
              {row.map((cell, i) => (
                <td key={i} className="border-b px-3 py-1.5 align-top">
                  <Inline text={cell} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Answer({ text }) {
  const lines = text.split("\n");
  const blocks = [];
  for (let i = 0; i < lines.length; i++) {
    if (isRow(lines[i]) && isRule(lines[i + 1])) {
      const rows = [cells(lines[i])];
      for (i += 2; isRow(lines[i]); i++) rows.push(cells(lines[i]));
      i--;
      blocks.push(<Table key={blocks.length} rows={rows} />);
      continue;
    }
    const heading = /^#{1,4}\s+(.*)$/.exec(lines[i]);
    blocks.push(
      <p
        key={blocks.length}
        className={heading ? "font-semibold" : "min-h-[1lh]"}
      >
        <Inline text={heading ? heading[1] : lines[i]} />
      </p>,
    );
  }
  return (
    <div className="whitespace-pre-wrap leading-relaxed [overflow-wrap:anywhere]">
      {blocks}
    </div>
  );
}
