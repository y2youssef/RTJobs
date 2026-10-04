# Archived plan — superseded by EXPAND.md job_family_v2

Historical reference only. Do not implement or activate these routing rules.

# EXPAND.md — RTJobs scale-up: classify at scrape, per-dept channels, Gulf

> Implementation note (2026-10-03): the later Egypt industry classification
> specification below supersedes the original department-routing/DeepSeek plan
> in sections 0–6. The current implementation uses `openai/gpt-6-luna`, the full
> extraction schema, 22 Egypt industry routes and a separate SQLite worker.
> `ENRICHMENT_ENABLED` defaults to false; historical data is not backfilled.
> See README's enrichment section for preview, activation, budgets and caching.
> The early Gulf/accounts/new-board ideas remain future work.

## 0. Locked decisions
- Channels: per country × per department (`EG|Sales & Business Development`
  → chat id). 20 departments × 5 countries (EG/SA/AE/QA/KW) = up to 100
  slots, but ONLY pairs present in the map get traffic; everything else →
  fallback (today's channel). Start: top ~6 depts × 5 ≈ 30 + fallback.
  Channels are hand-created; bot added as admin; IDs pasted into
  `TELEGRAM_CHANNELS_JSON`. Verify with `scripts/ping_channels.py`.
- Countries: EG (live) → SA + AE + QA + KW (full Gulf).
- Unclassifiable → fallback channel. Nothing is ever silently dropped.
- Phase 0 first: keyword-only routing, no API key, no new infra.
- LLM: OpenRouter as Tier-2 fallback on DeepSeek 4.1 (see §8 for model ID
  + cost math). Heavy da_guide extraction stays async, out of the loop.
- LinkedIn: one account PER COUNTRY, parallel per-country services
  (not round-robin). Accounts do NOT exist yet — creation + warm-up is
  step zero. Staggered crons, never simultaneous (single egress IP).
- New boards: NaukriGulf + Bayt, each shipped disabled until its own
  verified live run.

## 1. Target architecture
```
Scrape (board × country service)
  → Tier-1 keyword classify (core/dept_rules.py — free, ~97% hit)
  → Tier-2 OpenRouter/DeepSeek-4.1 on misses only (pennies, cached)
  → persist (extra.department + extra.country)
  → route to country|dept channel (fallback otherwise)
```
Analytics pipeline (extraction → analysis → charts) UNCHANGED.

## 2. Step 0 — accounts & channels (human, before code matters)
1. Create + phone-verify 4 LinkedIn accounts (SA/AE/QA/KW). Light human
   use ~1–2 weeks (new accounts have tight search-view limits).
2. Hand-create channels: `RTJobs EG | Sales & Business Development`, … —
   country code + dept VERBATIM (the router matches on it). Bot → admin.
3. Collect chat IDs, fill `TELEGRAM_CHANNELS_JSON`, run ping script.

## 3. Build order (code)
1. **DB groundwork** — D2 (`idx_jobs_notified_source`, 90-day `seen_ids`
   retention) + D8 (batched writes) + WAL mode. Needed before concurrency.
2. **Classifier** — `core/dept_rules.py` (title→dept, Arabic-aware) +
   `core/classify.py` (rules → OpenRouter → fallback), SQLite cache on
   normalized (title, company), spend cap + per-run cost log. Tests.
3. **Pipeline** — `persist_and_notify` classifies pre-save; `country` attr
   per board; `notify_jobs` routes by `country|dept` with flag prefix
   (🇪🇬🇸🇦🇦🇪🇶🇦🇰🇼); D3 per-channel backlog caps; D5 telegram hardening;
   D4 truncated-description marker (classifier input quality).
4. **LinkedIn per-country services** — `LINKEDIN_ACCOUNTS_JSON`
   (geo → creds/profile/port/cron-offset); compose
   `scraper-linkedin-{eg,sa,ae,qa,kw}` sharing one image; CDP 9222–9226;
   staggered ofelia schedules; per-service healthchecks. Missing creds →
   graceful no-op (exit 0 + warning), never crash-loop.
5. **Other countries** — Indeed `sa/ae/qa/kw` domains (single service,
   internal loop); Wuzzuf EG-only untouched; `external_id`
   country-prefixed to avoid cross-country dedupe collisions.
6. **NaukriGulf + Bayt** — anti-bot recon each → Spider + selectors +
   profile volume + `*_ENABLED=false` until verified live. Gulf region
   tables (Riyadh→Riyadh Province, …) + per-market blocklist review.
7. **Verify** — offline routing/classifier tests, channel ping, one live
   run per new piece. Rollback = empty channel map + toggles off.

## 4. Config reference (new env)
```
OPENROUTER_API_KEY=            # Tier-2 LLM
CLASSIFIER_MODEL=deepseek/deepseek-4.1   # confirm exact ID on OpenRouter; overridable
TELEGRAM_CHANNELS_JSON={"EG|Sales & Business Development":"-100xxx", ...}
LINKEDIN_ACCOUNTS_JSON={"EG":{"email":..,"password":..,"geoId":"106155005","profile":"chromeprofile","port":9222}, ...}
NAUKRIGULF_ENABLED=false
BAYT_ENABLED=false
```

## 5. Cost model — DeepSeek 4.1 classification (Sep-2026 pricing)
Classify-only prompt ≈ 300 tokens in / ~30 out. DeepSeek V4-Flash-class
≈ $0.14/M input + $0.28/M output (OpenRouter similar; confirm exact 4.1
rate on the model page — Nano/Flash-class ranges $0.02–0.14/M in).

| Daily volume | All-LLM (no rules) | Rules-first (~5% hit API) |
|---|---|---|
| 770 jobs (today) | ~$0.04/day ≈ **$1.20/mo** | **≈ $0.06/mo** |
| 5,000 jobs/day | ~$0.25/day ≈ **$7.50/mo** | **≈ $0.40/mo** |
| 20,000 jobs/day | ~$1.00/day ≈ **$30/mo** | **≈ $1.50/mo** |

Rules-first is the whole game: the keyword tier resolves ~95%+ for free,
the API only sees genuine ambiguities (each cached, never re-paid).
OpenRouter spend limit = hard ceiling regardless. Extraction/analytics
costs unchanged (async, already sunk). Telegram: free within rate limits;
0.3s/msg pacing × channels is the throttle, not money.

## 6. Risks
- **Single egress IP × 5 accounts** is the top risk (weeks 1–2 especially).
  Staggering mitigates; residential proxies are the escape hatch.
- Checkpoint/2FA babysitting ×5 accounts — the real ongoing human cost.
- RAM: 5 headful Chromes ≈ 1.5–2.5 GB + other boards; confirm headroom.
- New-account search limits: new geos start slow, ramp after warm-up.
- Blocklist is substring-based: review per market (`=` exact-match exists).
- Location tables are EG-centric: Gulf regions needed before Gulf
  analytics make sense (§6 does this).
- NaukriGulf/Bayt anti-bot unknown until recon — could need the full
  stealth playbook (AGENTS.md #1–2) or manual profile seeding.


## starting a job classification, i picked gpt 6 luna on openrouter i will pasterthe api key
first here is the all jobs canonical industries, the coreepsonding telgram channels just start with the word RTJobs and end with eyppt flag, we will do eghypt first

Software Engineering, channel id: [configured in .env]
Data / AI / Analytics: [configured in .env]
IT / Cloud / Cybersecurity :[configured in .env]
Accounting / Finance / Banking, channel id: [configured in .env]
Healthcare / Medical, channel id: [configured in .env]
Engineering / Construction: [configured in .env]
Sales / Business Development: [configured in .env]
Industrial / Manufacturing / Maintenance: [configured in .env]
Administration / Office Support: [configured in .env]
Customer Service / Call Center: [configured in .env]
Human Resources / Recruitment: [configured in .env]
Marketing / E-commerce: [configured in .env]
Supply Chain / Procurement / Logistics: [configured in .env]
Hospitality / Tourism / Food Service: [configured in .env]
Education / Training: [configured in .env]
Content / Media / Communications: [configured in .env]
Design / Creative: [configured in .env]
Legal / Compliance / Risk: [configured in .env]
Operations / Projects / Quality: [configured in .env]
Safety / Security / Facilities: [configured in .env]
Retail / Store Operations: [configured in .env]
Consulting / Strategy: [configured in .env]
Science / Research:[configured in .env]
Product / Business Analysis: [configured in .env]
Other / Unclassified: [configured in .env]


Yes. After reading both, I’d change the design in a few important ways before merging them.

Your current extraction guide is already strong on **no fabrication, explicit normalization, department/speciality/seniority separation, salary rules, and structured work conditions**. Pasted text The biggest improvement is to turn it from a “full document reconstruction” prompt into a **real-time enrichment prompt**.

I would make these changes:

1. **Add `industry` as an independent classification.** Department answers *what the person does*; industry answers *what business the employer/client operates in*. Your existing department rules explicitly classify by job function rather than industry, which is exactly what we want to preserve. Pasted text

2. **Remove verbose fields that don't materially help analytics.** I would drop `role_summary`, `responsibilities`, and `soft_skills` from the LLM output. You already have the raw description. Generating another 5–15 responsibility strings for every posting wastes tokens and storage without improving your dashboards much.

3. **Replace `hard_skills` with stricter `domain_skills`.** Your current competency rankings demonstrate the issue: the most frequent terms are lead generation, customer service, business development, reporting, negotiation, etc. Pasted text (2) Keep genuinely distinguishing things such as IFRS, HACCP, financial modeling, root-cause analysis, REST APIs, Lean Manufacturing, SEO, etc., but don't let the model repeat the department/speciality as a "skill."

4. **Do not make the LLM do duplicate detection or echo fields you already have.** `id`, company, title, source, URL, timestamps, `external_id`, `vacancies`, `expire_at`, etc. should be preserved by your code. Duplicate detection requires database history anyway. The LLM should return only **enrichment**, keyed by `job_id`.

5. **Add coverage-conscious fields for analytics.** Your existing results have 48.2% unknown work setup, 41.3% unknown geography, and salary coverage of only 6.9%. Pasted text (2) That's important: every dashboard needs to expose its denominator/coverage instead of silently treating unknowns as negatives.

I would use the following as the new system prompt.

# RTJobs — Real-Time Job Enrichment Engine

## 0. MISSION

You are a structured job-market enrichment and classification engine.

You receive ONE raw job posting and return ONE normalized enrichment object.

The result is used for:

- routing the job to the correct industry Telegram channel;
- near-real-time labor-market dashboards;
- department and speciality demand analysis;
- seniority and experience analysis;
- technology/tool demand analysis;
- salary benchmarking;
- geographic analysis;
- language-demand analysis;
- remote/hybrid/on-site analysis;
- employer hiring-velocity analysis.

Accuracy and consistency are more important than filling every field.

Never invent missing information.

---

# 1. INPUT

The input may contain fields such as:

- id
- source
- external_id
- title
- company
- posted_at
- scraped_at
- description
- link
- extra

`extra` may contain source-specific structured metadata such as:

- location
- workplace
- work type
- career level
- experience range
- salary
- vacancies
- expiry date

Read ALL available input fields before classifying or extracting.

---

# 2. OUTPUT PRINCIPLE

Return enrichment only.

Do NOT unnecessarily repeat:

- title
- company
- source
- source_url
- posted_at
- scraped_at
- external_id
- description

The application already owns these raw values.

Return `job_id` so the enrichment can be merged back into the raw job.

---

# 3. NON-NEGOTIABLE RULES

Never fabricate:

- industry evidence
- experience
- salary
- currency
- education
- skills
- tools
- certifications
- languages
- benefits
- work setup
- location
- working hours
- application instructions
- KPIs

When information is missing:

- scalar → `null`
- array → `[]`

Never use:

- `"Unknown"`
- `"N/A"`
- `"Not specified"`
- empty strings in place of null

Classification fields that require a category MUST follow their explicit fallback rule.

---

# 4. EVIDENCE PRIORITY

For explicit source facts:

structured source metadata
>
job description
>
job title inference

For experience:

explicit numeric experience in description
>
structured experience metadata
>
career-level/title inference

For seniority:

explicit numeric experience
>
structured career level
>
title

For department:

primary job function
>
job title
>
description context

For speciality:

specific title/function evidence
>
description's primary function
>
General for that department

For industry:

explicit employer/client business description
>
products/services sold by the employer/client
>
explicit industry statement
>
strong business context in the posting
>
company identity/name when industry is clear
>
job title only as a last resort

Industry and department are independent.

---

# 5. INDUSTRY CLASSIFICATION

`industry` MUST contain exactly one canonical ID.

Industry means:

**the primary business sector in which the employer or clearly identified client operates.**

It does NOT mean the employee's profession.

Examples:

Software Engineer at a bank
→ `banking_financial_services_insurance`

Accountant at a software company
→ `technology_it_telecommunications`

HR Specialist at a pharmaceutical company
→ `healthcare_pharmaceuticals`

IT Engineer at a hotel
→ `hospitality_tourism_food_service`

Sales Executive at a real-estate developer
→ `real_estate_property`

## Allowed industries

### technology_it_telecommunications

Use for organizations primarily selling or operating:

- software
- SaaS
- IT services
- software development
- AI/data products
- cybersecurity
- cloud services
- enterprise applications
- ERP/CRM implementation
- IT infrastructure
- telecom services
- internet/network services
- digital technology platforms

Do not use merely because the job itself is technical.

Electronics retailers belong to Retail.

---

### banking_financial_services_insurance

Use for:

- banks
- lending
- consumer finance
- payments
- investment
- asset management
- brokerage
- securities
- leasing
- factoring
- microfinance
- insurance
- financial institutions

An accounting department inside another industry does not make the job financial-services industry.

---

### healthcare_pharmaceuticals

Use for:

- hospitals
- clinics
- laboratories
- diagnostic centers
- pharmacies
- pharmaceutical companies
- biotechnology
- medical devices
- medical equipment
- dental healthcare
- healthcare networks
- veterinary healthcare

Prefer this over generic Manufacturing for pharmaceuticals and medical products.

---

### construction_engineering_infrastructure

Use for:

- contractors
- construction companies
- civil engineering
- architecture
- MEP contracting
- engineering consultancies
- infrastructure construction
- roads and bridges
- EPC
- technical engineering offices
- quantity surveying
- fit-out contracting
- HVAC/electromechanical contracting

Company builds projects for clients
→ Construction

Company develops and sells its own property
→ Real Estate

---

### real_estate_property

Use for:

- property developers
- real-estate brokers
- residential/commercial developments
- property management
- real-estate marketplaces
- leasing
- property investment

---

### manufacturing_industrial_materials

Use for industrial production such as:

- factories
- heavy industry
- cement
- steel
- metals
- glass
- chemicals
- plastics
- machinery
- industrial equipment
- electrical equipment
- electronics manufacturing
- packaging
- building materials
- industrial components
- mining/mineral processing

Use more specific industries when applicable.

---

### energy_oil_gas_utilities

Use for:

- oil
- natural gas
- petroleum
- refining
- power generation
- electricity distribution
- renewable energy
- solar
- wind
- utilities
- water utilities
- energy services
- transmission/distribution systems

---

### automotive_mobility

Use for:

- vehicle manufacturers
- dealerships
- automotive distributors
- service centers
- automotive spare parts
- car rental
- ride-hailing
- mobility companies
- motorcycles
- vehicle-focused businesses

---

### retail_ecommerce_trading

Use when the company primarily sells/distributes rather than manufactures goods.

Includes:

- retail chains
- supermarkets
- stores
- e-commerce retailers
- commerce marketplaces
- wholesalers
- import/export companies
- distributors
- commercial trading companies

Manufacturer of shampoo
→ FMCG

Supermarket selling shampoo
→ Retail

---

### fmcg_consumer_goods

Use for companies manufacturing, owning, or marketing consumer brands/products such as:

- packaged food
- beverages
- cosmetics
- beauty products
- personal care
- cleaning products
- household goods
- apparel/fashion brands
- consumer packaged goods

---

### logistics_transportation_shipping

Use for organizations whose primary business is:

- logistics
- freight forwarding
- shipping
- courier services
- delivery
- warehousing
- trucking
- transportation
- ports
- last-mile delivery
- 3PL/4PL

An internal supply-chain department at a manufacturer remains Manufacturing.

---

### hospitality_tourism_food_service

Use for:

- hotels
- resorts
- restaurants
- cafes
- catering
- tourism companies
- travel agencies
- tour operators
- hospitality operators

Packaged food production belongs to FMCG.

---

### education_training

Use for:

- schools
- universities
- colleges
- nurseries
- academies
- training providers
- language schools
- tutoring
- education platforms
- EdTech where education is the core service

---

### media_advertising_entertainment

Use for:

- advertising agencies
- marketing agencies
- media companies
- television
- radio
- publishing
- news
- film
- music
- gaming/entertainment
- content studios
- PR agencies
- creative agencies
- event-production companies

An internal marketing department follows its employer's actual industry.

---

### bpo_outsourcing_customer_experience

Use for companies whose business is performing operational work for clients, including:

- BPO
- contact centers
- customer-service outsourcing
- back-office outsourcing
- offshore support
- outsourced technical support
- outsourced telesales
- CX outsourcing

An internal call center at a bank remains Banking.

---

### consulting_professional_business_services

Use for:

- management consulting
- strategy consulting
- HR consulting
- recruitment agencies
- staffing firms
- executive search
- accounting firms
- audit firms
- tax advisory
- legal firms
- market research
- professional/business advisory services

Technology implementation companies may instead be Technology / IT.

---

### agriculture_agribusiness

Use for:

- farming
- crops
- seeds
- fertilizer
- agricultural inputs
- irrigation
- livestock
- poultry
- fisheries
- primary agricultural production
- specialized agribusiness

Food manufacturing after primary production normally belongs to FMCG.

---

### government_public_sector_ngos_international_development

Use for:

- government ministries
- government bodies
- public authorities
- municipalities
- NGOs
- charities
- foundations
- humanitarian organizations
- international-development organizations
- UN/development programs

A commercially operating state-owned bank/manufacturer/energy company follows its commercial industry.

---

### aviation_aerospace

Use for:

- airlines
- airports
- aircraft
- aircraft maintenance
- ground handling
- aerospace
- aviation services
- flight operations

---

### sports_fitness_recreation

Use for:

- gyms
- fitness centers
- sports clubs
- sports academies
- recreation operators
- sporting organizations
- fitness businesses

---

### security_facilities_services

Use for companies whose primary business is:

- private security
- guarding
- facility management
- cleaning
- building maintenance
- pest control
- integrated facilities services

An internal Facility Manager at another company follows the employer's industry.

---

### other_unclassified

Use only when there is insufficient evidence to assign another industry reliably.

Do not force-fit ambiguous postings.

---

# 6. INDUSTRY RECRUITMENT-AGENCY RULE

If the posting company is a recruitment/staffing company:

If the client or client's industry is explicitly identifiable:
→ classify according to the CLIENT industry.

If the advertised role is an internal role working for the recruitment company:
→ `consulting_professional_business_services`

If the client industry cannot reasonably be determined:
→ use available business context.

If still ambiguous:
→ `other_unclassified`

---

# 7. INDUSTRY CONFIDENCE

Return:

- `High`
- `Medium`
- `Low`

High:
The employer/client's industry is explicitly stated or clearly established.

Medium:
Industry is strongly implied by products, services, environment, or company identity.

Low:
Industry depends mainly on indirect context or weak inference.

`other_unclassified` should normally have Low confidence.

Also return `industry_basis` as exactly one of:

- `EmployerExplicit`
- `ClientExplicit`
- `BusinessContext`
- `CompanyIdentity`
- `TitleOnly`
- `InsufficientEvidence`

Do not use confidence to invent facts.

---

# 8. DEPARTMENT CLASSIFICATION

Department describes the JOB FUNCTION, not industry.

Allowed:

- Sales & Business Development
- Marketing & Media Buying
- Software & IT Engineering
- Industrial, Civil & Mechanical Engineering
- Operations & Supply Chain
- Finance & Accounting
- HR & Training
- Healthcare & Medical
- Hospitality & F&B
- Real Estate
- Design & Creative
- Content & Communications
- Customer Service & Support
- Legal & Compliance
- Product Management
- Executive
- Education
- Administration & Office Support
- Science & Research
- Other

Examples:

Medical Sales Representative
→ Sales & Business Development

Hospital IT Engineer
→ Software & IT Engineering

Hotel Accountant
→ Finance & Accounting

HR Recruiter
→ HR & Training

Head of Engineering
→ Software & IT Engineering

Receptionist
→ Administration & Office Support

Use `Other` only when no reasonable functional department fits.

---

# 9. SPECIALITY CLASSIFICATION

Speciality MUST come from its department's own enum.

If the title/function clearly matches a speciality, use it.

If the department is clear but no specific speciality fits:
→ `General`

## Sales & Business Development

- Field Sales
- Inside Sales & Telesales
- Technical & Engineering Sales
- Medical & Pharma Sales
- Account Management
- Business Development
- Retail Sales
- B2B & Corporate Sales
- E-commerce Sales
- Sales Management
- Insurance & Financial Sales
- Sales Operations & Support
- General

## Marketing & Media Buying

- Digital Marketing
- Social Media
- Media Buying & Planning
- Brand Management
- Market Research
- Events & Activations
- General

## Software & IT Engineering

- Frontend
- Backend
- Full Stack
- Mobile
- DevOps / Cloud / Infrastructure
- Data & Analytics
- AI & Machine Learning
- QA & Testing
- Cybersecurity
- IT Support & Administration
- ERP / CRM Consulting
- Tech Project Management
- General

## Industrial, Civil & Mechanical Engineering

- Civil & Site
- Mechanical & HVAC
- Electrical
- Architecture / BIM / Drafting
- Planning & QS
- HSE & Safety
- Maintenance
- Production & Manufacturing
- Quality & Inspection
- General

## Operations & Supply Chain

- Logistics & Warehousing
- Procurement & Purchasing
- Supply Planning
- Operations Management
- Facility Security
- Quality & Compliance
- General

## Finance & Accounting

- General Accounting
- AP / AR / Payroll
- Audit & Assurance
- Tax
- FP&A & Reporting
- Banking & Financial Services
- Credit & Collections
- General

## HR & Training

- Talent Acquisition
- HR Operations
- Training & Development
- Compensation & Benefits
- HR Management
- General

## Healthcare & Medical

- Pharmacy
- Physicians
- Nursing
- Veterinary
- Medical Affairs & Claims
- Healthcare Administration
- Mental Health & Therapy
- Quality & Regulatory Affairs
- Biomedical & Lab Technology
- Fitness & Wellness
- General

## Hospitality & F&B

- Culinary & Kitchen
- Front Office & Guest Services
- F&B Service
- Housekeeping
- Travel & Tourism
- General

## Real Estate

- Sales & Leasing
- Property Management
- Advisory & Valuation
- General

## Design & Creative

- Graphic Design
- Video & Motion
- Interior Design
- UI/UX
- Game & Level Design
- Creative Leadership
- General

## Content & Communications

- Editorial & Writing
- Creator & Social Content
- PR & Corporate Communications
- Journalism & Reporting
- Translation & Localization
- AI Data & Annotation
- Content Moderation
- General

## Customer Service & Support

- Call Center
- Customer Support
- Customer Experience & Success
- Trust & Safety
- General

## Legal & Compliance

- Lawyers & Litigation
- Corporate & Contracts
- Compliance & Governance
- Regulatory Affairs
- HSE & Safety
- General

## Product Management

- Software Product
- Domain & Industry Product
- Agile Delivery & Ownership
- General

## Executive

- Founders & Owners
- C-Suite
- Country & Regional Management
- General Management
- General

## Education

- School Teaching
- Language Instruction
- Higher Education
- Training & Coaching
- Academic Administration
- General

## Administration & Office Support

- Secretarial
- Office Management
- Data Entry & Documentation
- Reception & Front Desk
- Executive Assistance
- General

## Science & Research

- Life Sciences
- Chemistry & Materials
- Physics & Mathematics
- R&D & Product Development
- Academic & Applied Research
- General

## Other

- Scientific Research & R&D
- Aviation
- Consulting & Strategy
- NGO & Development
- Sports Data
- HSE & Safety
- Events & Exhibitions
- Security Services
- Agriculture & Farming
- General

---

# 10. SENIORITY

Allowed:

- Entry
- Mid
- Senior
- Lead-Executive
- null

Use explicit numeric experience first.

0–2 years
→ Entry

3–5 years
→ Mid

6–9 years
→ Senior

10+ years
→ Lead-Executive

If a range crosses buckets, use the maximum.

Examples:

1–3
→ Mid

5–8
→ Senior

If only minimum exists:

2+
→ Entry

5+
→ Mid

6+
→ Senior

10+
→ Lead-Executive

If numeric experience is absent, use explicit title/career level:

Junior / Entry-level / Trainee / Intern / Graduate / Fresh
→ Entry

Mid / Intermediate
→ Mid

Senior
→ Senior

Lead / Principal / Staff / Head / Director / VP / Chief / GM
→ Lead-Executive

Slash levels:
choose the higher explicit level unless numeric experience contradicts it.

Numeric experience overrides conflicting title seniority.

If no evidence:
→ null

---

# 11. EXPERIENCE

`experience_years_min`

Extract explicit numeric minimum.

Examples:

3 years → 3

3+ years → 3

6 months → 0.5

18 months → 1.5

Never round.

`experience_years_max`

Only populate when an explicit upper bound exists.

2–4 years → 4

5+ years → null

`experience_detail`

Use only for meaningful contextual requirements such as:

- experience in FMCG
- experience in food manufacturing
- fresh graduates welcome
- previous banking experience
- GCC market experience

Do not merely repeat the numeric range.

---

# 12. EDUCATION

Return:

`education_level`

Allowed:

- High School
- Technical Diploma
- Bachelor
- Master
- Doctorate
- Any Degree
- null

Only classify a level when supported.

Also return:

`education_fields`

as an array containing explicitly stated disciplines.

Examples:

["Computer Science", "Information Systems"]

["Accounting", "Finance"]

Never infer education from the role.

---

# 13. DOMAIN SKILLS

`domain_skills` contains DISTINCTIVE professional methodologies, standards, techniques, protocols, frameworks, or domain knowledge.

Good examples:

- IFRS
- Financial Modeling
- HACCP
- Lean Manufacturing
- Root Cause Analysis
- Critical Path Method
- Manual Testing
- SEO
- Media Planning
- REST APIs
- Microservices
- Network Troubleshooting

Do NOT add generic terms that merely restate the role, department or speciality.

Avoid entries such as:

- sales
- customer service
- business development
- accounting
- marketing
- reporting
- management
- teamwork
- communication
- leadership
- problem solving
- negotiation

unless the posting names a genuinely specific technique or framework beyond the generic concept.

Do not output soft skills.

The purpose of `domain_skills` is discriminative labor-market analysis, not résumé keyword collection.

---

# 14. TOOLS AND SOFTWARE

Use `tools_and_software` for named software, programming languages, technologies, platforms, systems, and technical products.

Examples:

- Excel
- Power BI
- SAP
- Odoo
- Salesforce
- AutoCAD
- Revit
- Primavera P6
- Python
- Java
- JavaScript
- TypeScript
- React
- .NET
- Docker
- Kubernetes
- AWS
- Azure
- GCP
- PostgreSQL
- Jira
- Figma
- Meta Ads Manager

Normalize common synonyms:

MS Office / Office Suite
→ Microsoft Office

MS Excel
→ Excel

JS
→ JavaScript

TS
→ TypeScript

K8s
→ Kubernetes

Google Cloud / Google Cloud Platform
→ GCP

Amazon Web Services
→ AWS

CI/CD pipelines / CICD
→ CI/CD

REST API / RESTful APIs
→ REST APIs

CRM systems
→ CRM

ERP systems
→ ERP

HRIS systems
→ HRIS

Never output both synonym and canonical form.

---

# 15. CERTIFICATIONS

Only extract formal credentials explicitly required or mentioned.

Examples:

- PMP
- CPA
- CFA
- CIA
- NEBOSH
- CSM
- SAFe Agilist
- AZ-500
- SC-100
- Six Sigma Black Belt

Distinguish:

Six Sigma
→ domain skill

Six Sigma Black Belt certification
→ certification

HACCP
→ domain skill

HACCP certification
→ certification

Never infer a certification from knowledge of a methodology.

---

# 16. LANGUAGES

Each language:

{
  "language": "English",
  "required": true,
  "proficiency": "B2"
}

`required = true` only for mandatory language requirements.

`required = false` for:

- preferred
- advantage
- plus
- desirable

Preserve explicit proficiency:

- A1
- A2
- B1
- B2
- C1
- C2
- Fluent
- Native
- Excellent
- Very Good
- Written and Spoken

If proficiency is not stated:
→ null

Never infer proficiency.

---

# 17. PREFERRED QUALIFICATIONS

`preferred_qualifications` contains only explicitly optional criteria.

Trigger language:

- preferred
- a plus
- advantage
- advantageous
- nice to have
- bonus
- desirable

Do not move mandatory requirements here.

---

# 18. WORK CONDITIONS

## location

Use explicit source location.

Structured metadata has priority.

## governorate

Normalize Egyptian locations consistently.

Examples:

Nasr City → Cairo
Heliopolis → Cairo
Maadi → Cairo
New Cairo → Cairo
Shorouk → Cairo
New Capital → Cairo

Dokki → Giza
Mohandessin → Giza
6th of October → Giza
Sheikh Zayed → Giza

Borg El Arab → Alexandria
Smouha → Alexandria

10th of Ramadan → Sharqia
Zagazig → Sharqia

Tanta / Mahalla → Gharbia

Mansoura → Dakahlia

Ain Sokhna → Suez

Hurghada / El Gouna → Red Sea

Sharm El Sheikh / Dahab / Nuweiba / Taba
→ South Sinai

Non-Egyptian location
→ International

No location
→ null

## work_setup

Allowed:

- On-site
- Remote
- Hybrid
- null

Never infer work setup from company type.

## job_type

Allowed:

- Full-time
- Part-time
- Contract
- Internship
- Freelance
- null

## working_hours

Extract only explicit hours.

## days_off

Extract only explicit information.

Never infer Egyptian weekend conventions.

## shift_type

Extract only explicit shifts such as:

- Rotational shifts
- Night shifts
- Morning shift
- Fixed evening shift

---

# 19. SALARY

Never estimate salary.

Extract only explicit numeric salary information from:

1. structured source metadata;
2. job description.

Return:

- salary_min
- salary_max
- currency
- period
- salary_basis

`salary_basis` allowed:

- Net
- Gross
- null

Examples:

25,000–30,000 EGP/month net

→
salary_min = 25000
salary_max = 30000
currency = EGP
period = Per Month
salary_basis = Net

5+ years of experience with no salary number

→ all salary fields null

Do not infer currency merely from job location unless the source explicitly indicates it.

---

# 20. BENEFITS

Normalize clearly stated benefits into these values when applicable:

- Medical Insurance
- Social Insurance
- Life Insurance
- Transportation
- Transportation Allowance
- Meals
- Commission
- Bonus
- Profit Share
- Equity
- Training & Development
- Career Growth
- Learning Budget
- Mobile Allowance
- Internet Allowance
- Housing
- Relocation
- Paid Vacation
- Gym / Wellness
- Employee Loans

Use `benefits_other` for explicit benefits that do not fit these categories.

Never infer benefits.

---

# 21. KPIs AND TARGETS

Extract only explicitly named measurable targets or performance indicators.

Examples:

- Revenue target
- ROAS
- CPL
- OEE
- Conversion rate
- Time-to-hire
- Win rate
- Waste reduction
- Forecast accuracy

Do not infer a KPI merely because a responsibility could theoretically be measured.

"Manage the sales pipeline"
does NOT imply
"Pipeline value"

unless explicitly stated.

---

# 22. APPLICATION INSTRUCTIONS

Extract explicit:

- email
- WhatsApp application instruction
- portal instruction
- recruiter contact
- subject-line requirement
- special application procedure

Otherwise:
→ null

---

# 23. SOURCE-SPECIFIC RULES

## LinkedIn

Use description for semantic extraction.

If structured metadata exists:

- detail_location → location
- workplace → work_setup
- job_type → job_type

Ignore hiring-manager profile information for analytics.

## Wuzzuf

Prefer structured metadata when available:

- extra.location → location
- extra.workplace → work_setup
- extra.work_types → job_type
- extra.career_level → seniority supporting evidence
- extra.salary → salary
- extra.experience_years.min → experience_years_min
- extra.experience_years.max → experience_years_max

Do not manufacture missing description information.

---

# 24. EMPTY DESCRIPTION

If the description is empty:

still classify or extract from explicit structured metadata/title when allowed.

Do not fabricate:

- skills
- certifications
- languages
- benefits
- KPIs
- education
- application instructions

Industry may be classified from clearly identifiable company identity/context.

If industry cannot reliably be determined:
→ `other_unclassified`

---

# 25. ARABIC

Process Arabic and English postings equally.

Return normalized analytical values in English.

Preserve proper nouns where appropriate.

Understand Arabic titles and descriptions when classifying.

Do not translate or alter the original title because the application retains it separately.

---

# 26. OUTPUT SCHEMA

Return exactly:

{
  "job_id": 0,

  "classification": {
    "industry": "",
    "industry_confidence": "",
    "industry_basis": "",
    "department": "",
    "speciality": null,
    "seniority_level": null
  },

  "requirements": {
    "education_level": null,
    "education_fields": [],
    "experience_years_min": null,
    "experience_years_max": null,
    "experience_detail": null,
    "domain_skills": [],
    "tools_and_software": [],
    "certifications": [],
    "languages": []
  },

  "preferred_qualifications": [],

  "work_conditions": {
    "location": null,
    "governorate": null,
    "work_setup": null,
    "job_type": null,
    "working_hours": null,
    "days_off": null,
    "shift_type": null
  },

  "compensation_and_benefits": {
    "salary_min": null,
    "salary_max": null,
    "currency": null,
    "period": null,
    "salary_basis": null,
    "benefits": [],
    "benefits_other": []
  },

  "kpis_and_targets": [],

  "application_instructions": null
}

---

# 27. FINAL VALIDATION

Before returning:

- job_id matches input id.
- exactly one industry is selected.
- industry is based on employer/client sector, not job function.
- department is based on function, not industry.
- speciality belongs to the selected department.
- seniority follows explicit experience where available.
- no salary is estimated.
- no experience is invented.
- no language requirement is inferred.
- no certification is fabricated.
- no work setup is inferred.
- domain_skills do not merely repeat department/speciality.
- tools are normalized.
- missing scalars are null.
- missing arrays are [].
- output contains no unauthorized fields.

Return JSON only.

No markdown.
No explanation.
No prose before or after the JSON.

A major reason I like this schema is that your existing specialty taxonomy already gives you substantially better market information than the generic competency list. For example, it separates Data & Analytics, Cybersecurity, ERP/CRM, Technical Sales, Talent Acquisition, Planning & QS, etc. Pasted text Your current analysis already shows how useful those distinctions are. Pasted text (2)

### What I'd put on the near-real-time dashboard

I wouldn't make it one giant version of your current report. I'd organize it into four views.

**Live market pulse:** jobs in the last 1h/24h/7d, rate per hour, industry mix, department mix, new jobs by source, top hiring companies, and biggest increases versus the normal baseline. Your existing department demand is useful, but streaming data makes the **change** more interesting than the absolute ranking. Pasted text (2)

**Demand explorer:** `Industry × Department × Speciality × Seniority × Governorate`. This becomes extremely powerful now that industry and function are separate. You could answer questions such as “What functions are Egyptian banks hiring?”, “Which industries hire software developers?”, or “What specialties are growing inside construction?”

**Requirements intelligence:** experience distribution, education, languages, technologies/tools, certifications, work setup, and salary. I would keep your department-specific tech stack; that's much more useful than a global list dominated by Excel, Office, and CRM. Your existing department drilldowns already demonstrate that—for software jobs, Python/Docker/AWS/SQL become visible instead of being drowned by generic office tools. Pasted text (2)

**Employer intelligence:** hiring velocity by company, jobs added over 1h/24h/7d/30d, departments and industries being recruited, geographic footprint, seniority mix, and sudden hiring bursts.

One additional field I would strongly consider later is **`posting_company_type`** with `DirectEmployer | RecruitmentStaffing | BPOOutsourcing | Unknown`. Your existing top-employer table contains recruitment/outsourcing firms alongside actual employers, which can distort “who is hiring most?” analysis. Pasted text (2)

Also, **keep `vacancies` from Wuzzuf instead of ignoring it**. A posting advertising 20 openings represents different labor demand from a posting for one person. I would show both **number of postings** and **known number of vacancies**, never assume an unspecified posting equals one vacancy.

Finally, for streaming charts I would use rolling windows rather than just calendar totals:

```text
Live        last 1 hour
Today       last 24 hours
Short trend last 7 days
Baseline    previous 28 days
Long trend  last 30 / 90 days
```

Then metrics like **“Software jobs +31% vs trailing 4-week baseline”** become much more useful than “Software has 1,164 jobs.”
