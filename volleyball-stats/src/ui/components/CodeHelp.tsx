const ROWS: [string, string][] = [
  ['12S+', '#12 serves in (S# ace, S= error). S+ alone = whoever is serving'],
  ['7R2', '#7 reception rated 2 (R3 perfect … R0 error, R alone = unrated)'],
  ['3A#', '#3 attack kill (A+ in play, A/ blocked, A= error)'],
  ['4B# 9B#', 'Shared block point (B+ touch, B= error)'],
  ['5D+', '#5 dig (D= dig error)'],
  ['1E=', 'Set error (normal sets are not entered)'],
  ['2F=', 'Free-ball / down-ball error'],
  ['A#', 'No number = team action, no player credited'],
  ['p', 'Our point, nobody credited (handy with score-only tracking)'],
  ['o', 'Opponent point (nobody of ours at fault)'],
  ['oe', 'Opponent error (our point)'],
  ['rc', 'Red card: point to the opponent'],
  ['to', 'Our timeout'],
  ['sub 11>4', '#11 in for #4 (inj 11>4 = injury substitution)'],
  ['lib', 'Libero on/off (lib 14 = make #14 the active libero)'],
];

export function CodeHelp({ compact }: { compact?: boolean }) {
  return (
    <div className={compact ? 'small' : undefined}>
      <p className="subtle small">
        Type codes separated by spaces and press <kbd>Enter</kbd>. A rally closes by itself when one of our actions ends it
        (ace, kill, block point, or any error). <kbd>Ctrl</kbd>+<kbd>Z</kbd> on an empty line undoes the last entry.
      </p>
      <table className="data">
        <tbody>
          {ROWS.map(([code, text]) => (
            <tr key={code}>
              <td className="left">
                <code>{code}</code>
              </td>
              <td className="left" style={{ whiteSpace: 'normal' }}>
                {text}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
