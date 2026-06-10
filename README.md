# IRINS Publication Scraper — IITD CARE Faculty

A robust, hybrid Python scraper designed to extract faculty profiles and publication records from the **Indian Research Information Network System (IRINS)** instance for IIT Delhi CARE. 

The script dynamically extracts the faculty member's name using a headless browser, pulls entire publication histories via optimized backend endpoints, enriches the metadata using the **CrossRef API**, and formats the final output into clean, web-ready HTML list items (`<li>`).

---

## 🚀 Key Features

* **Hybrid Architecture**: Uses **Playwright** (Chromium) exclusively to let Angular render the faculty name dynamically, while switching to lightning-fast **Requests POST payloads** to grab publication lists without browser overhead.
* **CrossRef Enrichment**: Automatically intercepts DOIs, queries CrossRef APIs to fetch authoritative metadata (precise co-author initializations, clean journal formatting, page spans, and exact publication months).
* **Smart Author De-duplication & Formatting**: Standardizes inconsistent name conventions across platforms into clean `A. Kumar; B. Sharma` styling.
* **Resilience & Politeness**: 
  * Features randomized backoff delay tracking (`DELAY_PAGES`, `DELAY_PROFILES`) to prevent IP throttling.
  * Robust HTTP status handling (`429`, `500+`) with linear-to-exponential retry waits.
* **Session Progress Tracking**: Writes checkpoints to `_progress.json`. If network connection drops or you manually trigger a `Ctrl+C` interrupt, the script safely picks up exactly where it left off.

---

## 🛠️ Installation & Setup

> ⚠️ **Important:** Do not run this script on cloud infrastructure (AWS, Google Cloud, GitHub Actions, etc.). IRINS aggressive scraping firewalls heavily filter or outright ban popular cloud data center IP blocks. **Run it on your local machine or campus network.**

1. **Clone the Repository:**
   
   ```bash
   
   git clone [https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git](https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git)

   cd YOUR_REPO_NAME

2. **Dependencies:**
   
   ```pip install requests beautifulsoup4 playwright```
   
   ```python -m playwright install chromium```

3. **Setup:**
   Open fetch.py and modify the FACULTY_IDS array configuration near the top of the file with the specific IRINS profile IDs you want to target:
   FACULTY_IDS = [
    "70201", "70513", "70039", # Add or replace target IDs here
]
Execute the pipeline script:

    ```python fetch.py```
