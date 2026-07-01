import { useCallback, useEffect, useState } from "react";

const THEME_KEY = "noeta-theme";

function storedTheme() {
  try {
    return window.localStorage.getItem(THEME_KEY);
  } catch (error) {
    return null;
  }
}

function systemTheme() {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  } catch (error) {
    return "light";
  }
}

function applyTheme(theme) {
  if (theme === "dark" || theme === "light") {
    document.documentElement.setAttribute("data-theme", theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
}

function useThemeToggle() {
  const [theme, setTheme] = useState(() => storedTheme() || systemTheme());

  useEffect(() => {
    applyTheme(theme);
    try {
      window.localStorage.setItem(THEME_KEY, theme);
    } catch (error) {}
  }, [theme]);

  const toggleTheme = useCallback(() => {
    setTheme((current) => (current === "dark" ? "light" : "dark"));
  }, []);

  return { theme, toggleTheme };
}

export { THEME_KEY, applyTheme, useThemeToggle };
