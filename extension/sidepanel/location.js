// Where the user is, sent with every task so Krify uses local sites (amazon.in) and currency.
// A. Country: guessed from the browser's time zone, no permission needed; the user can change it.
// B. Exact place: read only after the user presses "Use exact location"; ✕ forgets it.
//    The coordinates are turned into a city name by BigDataCloud's free reverse-geocoding service.

const $ = (id) => document.getElementById(id);

// Time zone → country code, for the most common zones. Others fall back to the browser language.
const ZONES = {
  "Asia/Kolkata": "IN", "Asia/Calcutta": "IN", "Asia/Dubai": "AE", "Asia/Karachi": "PK", "Asia/Dhaka": "BD",
  "Asia/Kathmandu": "NP", "Asia/Colombo": "LK", "Asia/Singapore": "SG", "Asia/Kuala_Lumpur": "MY",
  "Asia/Jakarta": "ID", "Asia/Manila": "PH", "Asia/Bangkok": "TH", "Asia/Tokyo": "JP", "Asia/Seoul": "KR",
  "Asia/Shanghai": "CN", "Asia/Hong_Kong": "HK", "Asia/Riyadh": "SA", "Asia/Qatar": "QA",
  "Europe/London": "GB", "Europe/Dublin": "IE", "Europe/Paris": "FR", "Europe/Berlin": "DE",
  "Europe/Madrid": "ES", "Europe/Rome": "IT", "Europe/Amsterdam": "NL", "Europe/Stockholm": "SE",
  "Europe/Zurich": "CH", "Europe/Warsaw": "PL", "Europe/Istanbul": "TR", "Europe/Moscow": "RU",
  "America/New_York": "US", "America/Chicago": "US", "America/Denver": "US", "America/Phoenix": "US",
  "America/Los_Angeles": "US", "America/Anchorage": "US", "Pacific/Honolulu": "US",
  "America/Toronto": "CA", "America/Vancouver": "CA", "America/Mexico_City": "MX", "America/Sao_Paulo": "BR",
  "America/Argentina/Buenos_Aires": "AR", "Australia/Sydney": "AU", "Australia/Melbourne": "AU",
  "Australia/Perth": "AU", "Pacific/Auckland": "NZ", "Africa/Lagos": "NG", "Africa/Nairobi": "KE",
  "Africa/Johannesburg": "ZA", "Africa/Cairo": "EG",
};
const COUNTRIES = [...new Set(Object.values(ZONES))];
const regionName = new Intl.DisplayNames(["en"], { type: "region" });

let here = { country: "", place: "" }; // country code, e.g. "IN"; place, e.g. "Hyderabad, Telangana, India"

function guessCountry() {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (ZONES[zone]) return ZONES[zone];
  const region = navigator.language.split("-")[1];
  return region && region.length === 2 ? region.toUpperCase() : "";
}

function show() {
  const select = $("countrySelect");
  const codes = [...new Set([here.country, ...COUNTRIES].filter(Boolean))];
  codes.sort((a, b) => regionName.of(a).localeCompare(regionName.of(b)));
  select.innerHTML = '<option value="">(not set)</option>';
  for (const code of codes) select.add(new Option(regionName.of(code), code));
  select.value = here.country;
  $("placeLabel").hidden = !here.place;
  $("placeText").textContent = here.place;
  $("gpsBtn").hidden = Boolean(here.place);
}

function save() {
  chrome.storage.local.set({ location: here });
  show();
}

async function useExactLocation(note) {
  // Chrome doesn't allow "geolocation" as an optional permission, so the extension has it, and this
  // button is where the user agrees: the location is read only after they press it.
  $("gpsBtn").disabled = true;
  note("📍 Finding your location (the city name comes from BigDataCloud)…");
  try {
    const pos = await new Promise((ok, fail) =>
      navigator.geolocation.getCurrentPosition(ok, fail, { timeout: 15000, maximumAge: 10 * 60 * 1000 }),
    );
    const { latitude, longitude } = pos.coords;
    const url = "https://api.bigdatacloud.net/data/reverse-geocode-client"
      + `?latitude=${latitude}&longitude=${longitude}&localityLanguage=en`;
    const d = await (await fetch(url)).json();
    here.place = [d.city || d.locality, d.principalSubdivision, d.countryName].filter(Boolean).join(", ");
    if (d.countryCode) here.country = d.countryCode;
    save();
    note(`📍 Location set: ${here.place}`);
  } catch (err) {
    note(`📍 Couldn't get your location: ${err.message || err}`);
  } finally {
    $("gpsBtn").disabled = false;
  }
}

function forgetPlace() {
  here.place = "";
  save();
}

/** Set up the location row. note(text) shows a line in the step log. */
export async function initLocation(note) {
  const { location: saved } = await chrome.storage.local.get("location");
  here = saved || { country: guessCountry(), place: "" };
  if (!saved) chrome.storage.local.set({ location: here });
  show();
  $("countrySelect").onchange = (e) => {
    here.country = e.target.value;
    here.place = ""; // the exact place belonged to the old country
    save();
  };
  $("gpsBtn").onclick = () => useExactLocation(note);
  $("forgetPlaceBtn").onclick = forgetPlace;
}

/** What the server gets with a task: {"country": "India", "place": "Hyderabad, Telangana, India"}. */
export function currentLocation() {
  return { country: here.country ? regionName.of(here.country) : "", place: here.place };
}
