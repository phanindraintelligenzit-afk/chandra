import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}"
  ],
  theme: {
    extend: {
      colors: {
        obsidian: "#050505",
        carbon: "#0d0d0f",
        graphite: "#17171b",
        signal: "#ff3b3b",
        amber: "#ffb347",
        operational: "#8ed9a8",
        frost: "#f4efe7",
        muted: "#928a80"
      },
      fontFamily: {
        display: ["var(--font-display)", "Georgia", "serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"]
      },
      boxShadow: {
        signal: "0 0 36px rgba(255, 59, 59, 0.28)",
        amber: "0 0 32px rgba(255, 179, 71, 0.22)"
      }
    }
  },
  plugins: []
};

export default config;
