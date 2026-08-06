"use client";

import { useEffect, useRef } from "react";

interface LogViewerProps {
  logs: string[];
  maxHeight?: string;
}

export function LogViewer({ logs, maxHeight = "400px" }: LogViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [logs]);

  return (
    <div
      ref={containerRef}
      className="bg-black/40 border border-white/10 rounded-lg p-3 font-mono text-xs text-frost/80 overflow-y-auto"
      style={{ maxHeight }}
    >
      {logs.length === 0 ? (
        <p className="text-frost/40 italic">No logs yet...</p>
      ) : (
        logs.map((line, i) => (
          <div key={i} className="py-0.5 whitespace-pre-wrap break-all">
            {line}
          </div>
        ))
      )}
    </div>
  );
}
