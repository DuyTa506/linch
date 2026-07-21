import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "@fontsource/jetbrains-mono/700.css";
import "./styles.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { studioApi } from "./api/client";
import { App } from "./App";

const host = document.getElementById("root");
if (!host) throw new Error("missing #root");

createRoot(host).render(
  <StrictMode>
    <App api={studioApi} />
  </StrictMode>,
);
