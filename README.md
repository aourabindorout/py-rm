# Python Roadmap (`py-rm`)

> **I made this to help my brother learn Python.**  
> **Only users created by me can login.**

**Live URL:** [https://learn-python.cloud.run](https://learn-python.cloud.run)

---

## 🎯 About The Project

`py-rm` is a personalized, distraction-free learning platform designed specifically for a 13-year-old beginner starting his programming journey. 

It organizes learning into manageable bite-sized milestones featuring:
- **Curated Topics**: Core Python fundamentals explained in simple, beginner-friendly terms.
- **Handpicked YouTube Lessons**: Embedded high-quality tutorials to watch directly on the platform.
- **Interactive Checklists**: Real-time progress tracking to keep momentum going.
- **Quizzes**: Fun checkpoints to test understanding after each module.

To keep the platform private and focused, there is **no public registration**. The entire application is locked behind an authentication barrier—the login screen is the only page exposed to the open internet.

---

## 🛠️ Technology & Tools

| Component | Tool / Technology | Reason for Choice |
| :--- | :--- | :--- |
| **Language & Backend** | **Python (Flask)** | Written in the very language he is learning, making the codebase itself an educational artifact. Flask is lightweight, easy to inspect, and fast to run. |
| **Templates & UI** | **Jinja2 + Tailwind CSS** | Server-side rendered pages for instant access control, styled with modern, clean, mobile-responsive UI components. |
| **Database** | **Google Cloud Firestore** | Serverless NoSQL document database. Fully covered under GCP's permanent free tier (1 GB storage, 50k reads/day), requiring zero VM maintenance and \$0 idle cost. |
| **Content Store** | **YAML / JSON** | Curriculum, video links, and quiz questions live in structured configuration files inside the repository for easy updates without database migrations. |
| **Hosting & Compute** | **Google Cloud Run** | Fully managed serverless container runtime. Scales to zero when not in use to avoid unwanted hosting fees. |
| **Containerization** | **Docker** | Encapsulates the runtime, dependencies, and configuration into a reproducible container image. |

---

## 🛡️ Guardrails: Protecting `GET /login` & `POST /login` Against Abuse

Since the login page is the sole entry point exposed to the public internet, guardrails are implemented to prevent brute-force credential attacks, spam, and resource exhaustion that could raise cloud costs:

### 1. Cloud Run Hard Limit (`max-instances = 1`)
- Google Cloud Run is configured with a strict ceiling of **1 maximum instance**.
- Even in the event of an automated traffic flood, GCP will **never** spin up additional instances. Excess requests are throttled or dropped, putting a hard ceiling on cloud compute costs.

### 2. Application-Level Rate Limiting (`Flask-Limiter`)
- **`POST /login` (Brute-Force & Credential Stuffing Guard)**:
  - **3 attempts per minute per IP address**.
  - **15 attempts per minute overall** across all visitors.
  - Subsequent requests receive an HTTP `429 Too Many Requests` status code with a cooldown period.
- **`GET /login` (Request Flood Guard)**:
  - **3 requests per minute per IP address**.
  - **25 requests per minute overall** across all visitors.

### 3. CPU Exhaustion & Timing Attack Prevention
- Passwords are encrypted using **bcrypt** with a calibrated cost factor.
- Rate-limiting checks evaluate **before** password hashing runs, ensuring malicious actors cannot consume server CPU by triggering thousands of simultaneous expensive hash computations.

### 4. Edge Protection (Cloudflare Free Tier / Cloud Armor)
- Placing Cloudflare in front of the Cloud Run URL filters out known malicious bots, mitigates DDoS attacks, and absorbs automated junk traffic at the edge before it ever reaches Google Cloud.

### 5. Global Authentication Wall
- All internal endpoints (`/`, `/topics`, `/quiz`, `/progress`) enforce strict session verification.
- Any unauthenticated request immediately returns a `302 Redirect` to `/login` without processing or returning internal application state.

### 6. Server-Side Session Invalidation & Inactivity Timeout
- **Instant Revocation on Logout**: Sessions are stored server-side with unique tokens. Clicking **Log Out** destroys the token in the database, ensuring any extracted or copied session cookies are instantly dead and cannot be reused (neutralizing replay attacks).
- **10-Minute Inactivity Expiry**: Sessions auto-expire if no requests are made for 10 minutes.
- **Device Fingerprint Binding**: The browser's User-Agent is hashed and validated against the session token to reject stolen tokens replayed on different browsers or devices.

