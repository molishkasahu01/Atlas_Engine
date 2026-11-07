/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#09090b",
        mint: "#a7f3d0",
        lemon: "#fde047",
        rose: "#fecaca",
      },
      fontFamily: {
        display: ["'Cabinet Grotesk'", "sans-serif"],
        sans: ["Satoshi", "sans-serif"],
        mono: ["'Fira Code'", "monospace"],
      },
      boxShadow: {
        "hard-sm": "2px 2px 0 0 #09090b",
        hard: "4px 4px 0 0 #09090b",
        "hard-lg": "8px 8px 0 0 #09090b",
      },
    },
  },
  plugins: [],
};