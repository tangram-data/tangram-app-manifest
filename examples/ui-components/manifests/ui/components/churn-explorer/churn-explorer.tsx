// Sandboxed component entry. Data bindings referenced here are declared in the
// component contract; actions are invoked directly through the governed
// performAction SDK. The platform compiles this file to a bundle at publish
// and auto-mounts the default export.
import { useEffect, useState } from "react";

const tangram = window.tangram;

export default function ChurnExplorer() {
  const [period, setPeriod] = useState<"7d" | "30d" | "90d">("30d");
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");

  useEffect(() => {
    let alive = true;
    setState("loading");
    tangram
      .query("churn_series", { period }) // named, contract-declared binding
      .then((r) => {
        if (alive) {
          setRows(r.rows);
          setState("ready");
        }
      })
      .catch(() => alive && setState("error"));
    return () => {
      alive = false;
    };
  }, [period]);

  if (state === "error") return <div>Couldn't load churn.</div>;
  return (
    <div>
      {(["7d", "30d", "90d"] as const).map((p) => (
        <button key={p} onClick={() => setPeriod(p)}>{p}</button>
      ))}
      <button
        onClick={() =>
          tangram.performAction(
            { resourceType: "Churn", action: "Export" },
            { parameters: { period } }
          )
        }
      >
        Export
      </button>
      <ul>
        {rows.map((d, i) => (
          <li key={i} onClick={() => tangram.emit("select", { month: String(d.month) })}>
            {String(d.month)}: {String(d.churned)}
          </li>
        ))}
      </ul>
    </div>
  );
}
