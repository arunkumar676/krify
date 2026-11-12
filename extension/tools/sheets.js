// Google Sheets: the grid is drawn on a canvas (no text in the page), so Krify downloads the open
// sheet tab as CSV instead – in the background, with the user's own login, nothing saved to disk.
// The server loads it into a small database (DuckDB) and answers the AI's questions from it.
// Read-only.

const SHEET_URL = /^https:\/\/docs\.google\.com\/spreadsheets\/(?:u\/(\d+)\/)?d\/([^/?#]+)/;
const MAX_BYTES = 5_000_000;

export const isSheetUrl = (url) => SHEET_URL.test(url || "");

/** The open sheet tab as CSV text. */
export async function sheetCsv(sheetUrl) {
  const [, account, id] = sheetUrl.match(SHEET_URL);
  const gid = (sheetUrl.match(/[#&?]gid=(\d+)/) || [])[1] || "0";
  const user = account !== undefined ? `/u/${account}` : "";
  const res = await fetch(`https://docs.google.com/spreadsheets${user}/d/${id}/export?format=csv&gid=${gid}`, {
    credentials: "include",
  });
  const text = await res.text();
  if (!res.ok || /^\s*</.test(text)) {
    throw new Error(`Couldn't download this sheet's data (${res.status}). Don't retry: tell the user.`);
  }
  if (text.length > MAX_BYTES) throw new Error("This sheet is too big for Krify to read (over 5 MB).");
  return text;
}
