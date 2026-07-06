"""
Management command: load_knowledge_base

Seeds the GeneralKnowledgeChunk table with curated Finnish health articles.
Content is based on Käypä hoito guidelines and Terveyskirjasto patient articles.

Usage:
    python manage.py load_knowledge_base
    python manage.py load_knowledge_base --clear   # wipe & re-index
"""
from django.core.management.base import BaseCommand


ARTICLES = [
    {
        'title': 'Understanding Creatinine and Kidney Function',
        'source_name': 'Terveyskirjasto',
        'source_url': 'https://www.terveyskirjasto.fi',
        'topic': 'kidney',
        'content': """
Creatinine is a waste product produced by normal muscle activity. Your kidneys filter creatinine
out of the blood and excrete it in urine. Blood creatinine is one of the most common tests used
to assess kidney function.

Normal creatinine ranges:
- Adult males: 62–115 µmol/L (0.7–1.3 mg/dL)
- Adult females: 44–97 µmol/L (0.5–1.1 mg/dL)

A rising creatinine over time may indicate declining kidney function. A single elevated reading
is not always alarming — dehydration, intense exercise, or certain medications can temporarily
raise creatinine. Trends over multiple tests are more meaningful than any single result.

When creatinine rises consistently, doctors often calculate eGFR (estimated Glomerular Filtration
Rate) to stage kidney function. eGFR above 90 is considered normal. Chronic Kidney Disease (CKD)
is classified in stages 1–5 based on eGFR levels.

If your creatinine is rising, your doctor may recommend:
- More frequent monitoring
- Blood pressure control
- Reduced dietary protein or salt
- Review of medications that affect kidney function (e.g. NSAIDs, certain antibiotics)
- Referral to a nephrologist if eGFR falls below 30

Always discuss abnormal creatinine results with your healthcare provider to understand the cause
and appropriate next steps. Do not adjust any medications based on lab results alone.
        """.strip(),
    },
    {
        'title': 'HbA1c — What It Measures and What Your Result Means',
        'source_name': 'Käypä hoito',
        'source_url': 'https://www.kaypahoito.fi',
        'topic': 'diabetes',
        'content': """
HbA1c (glycated haemoglobin) reflects your average blood glucose over the past 2–3 months.
It is the primary test used to diagnose and monitor type 2 diabetes in Finland.

Interpretation of HbA1c results (Finnish guidelines):
- Below 42 mmol/mol (6.0%): Normal — no diabetes
- 42–47 mmol/mol (6.0–6.4%): Prediabetes — increased risk, lifestyle changes recommended
- 48 mmol/mol (6.5%) or above: Diabetes — confirmed if repeated

For patients already diagnosed with type 2 diabetes, the treatment target is usually
HbA1c below 53 mmol/mol (7.0%), though the target may be individualised based on age,
other conditions, and risk of low blood sugar (hypoglycaemia).

A rising HbA1c trend means blood glucose control is worsening. A falling HbA1c after
starting medication or lifestyle changes shows improvement. The rate of change matters:
a sudden large rise may prompt medication adjustment; a gradual rise over years may reflect
natural disease progression.

HbA1c is usually rechecked every 3–6 months if treatment is being adjusted, and every
6–12 months once stable. Finnish Käypä hoito guidelines recommend annual HbA1c monitoring
for all patients with established type 2 diabetes.

Important: HbA1c can be falsely low in conditions like anaemia or haemolytic disorders.
Always interpret results together with your full clinical picture.
        """.strip(),
    },
    {
        'title': 'eGFR — Estimated Glomerular Filtration Rate Explained',
        'source_name': 'Terveyskirjasto',
        'source_url': 'https://www.terveyskirjasto.fi',
        'topic': 'kidney',
        'content': """
eGFR (estimated Glomerular Filtration Rate) measures how well your kidneys are filtering
blood per minute, adjusted for body surface area. It is calculated from your creatinine
level, age, and sex using a validated formula (CKD-EPI in Finland).

CKD staging by eGFR (KDIGO guidelines, used in Finnish hospitals):
- Stage 1: eGFR ≥ 90 mL/min — normal or high function (with other kidney damage present)
- Stage 2: eGFR 60–89 — mildly reduced
- Stage 3a: eGFR 45–59 — mildly to moderately reduced
- Stage 3b: eGFR 30–44 — moderately to severely reduced
- Stage 4: eGFR 15–29 — severely reduced
- Stage 5: eGFR < 15 — kidney failure (dialysis or transplant considered)

An eGFR below 60 for more than 3 months is the definition of Chronic Kidney Disease (CKD).
Rate of decline matters: losing more than 5 mL/min per year is considered rapid progression.

eGFR can fluctuate with hydration status and acute illness. A single low reading should
be repeated. Finnish Käypä hoito recommends that patients with eGFR below 60 are followed
at least annually, and below 30 every 3–6 months.

Medications often need dose adjustment when eGFR falls below 60 (e.g. metformin is usually
stopped below eGFR 30). Always inform all your healthcare providers of your kidney function.
        """.strip(),
    },
    {
        'title': 'High Blood Pressure (Hypertension) — Finnish Guidelines',
        'source_name': 'Käypä hoito',
        'source_url': 'https://www.kaypahoito.fi',
        'topic': 'hypertension',
        'content': """
Blood pressure is recorded as two numbers: systolic (the higher number, when the heart beats)
over diastolic (the lower number, between beats). It is measured in mmHg.

Blood pressure categories (Finnish Käypä hoito, aligned with ESC/ESH guidelines):
- Optimal: below 120/80 mmHg
- Normal: 120–129 / 80–84 mmHg
- High-normal: 130–139 / 85–89 mmHg
- Grade 1 hypertension: 140–159 / 90–99 mmHg
- Grade 2 hypertension: 160–179 / 100–109 mmHg
- Grade 3 hypertension: 180+ / 110+ mmHg

A single high reading does not diagnose hypertension — blood pressure varies throughout the
day and with stress, caffeine, or exercise. Diagnosis requires repeated elevated readings or
ambulatory (24-hour) blood pressure monitoring.

Untreated hypertension over years increases risk of stroke, heart attack, heart failure,
and kidney disease. Even modest reductions (5–10 mmHg) significantly reduce these risks.

Lifestyle measures that lower blood pressure:
- Reduce salt intake to under 5g/day
- Regular aerobic exercise (at least 150 min/week moderate intensity)
- Limit alcohol
- Maintain healthy weight
- Quit smoking

Medication is usually started at Grade 1 hypertension with cardiovascular risk, or Grade 2
regardless of risk. Common first-line medications in Finland: ACE inhibitors, ARBs,
calcium channel blockers, thiazide diuretics.
        """.strip(),
    },
    {
        'title': 'Cholesterol — Understanding Your Lipid Panel',
        'source_name': 'Käypä hoito',
        'source_url': 'https://www.kaypahoito.fi',
        'topic': 'cholesterol',
        'content': """
A lipid panel measures four values in your blood:
- Total cholesterol: sum of all cholesterol types
- LDL (low-density lipoprotein): "bad" cholesterol — carries cholesterol to arteries
- HDL (high-density lipoprotein): "good" cholesterol — carries cholesterol away from arteries
- Triglycerides: a type of blood fat, often elevated with poor diet or diabetes

Finnish target values depend on your cardiovascular risk level:
- Very high risk (known heart disease, diabetes with organ damage): LDL below 1.4 mmol/L
- High risk (diabetes, significant risk factors): LDL below 1.8 mmol/L
- Moderate risk: LDL below 2.6 mmol/L
- Low risk: LDL below 3.0 mmol/L

HDL should be above 1.0 mmol/L (men) or 1.2 mmol/L (women). Higher is protective.
Triglycerides above 1.7 mmol/L are considered elevated; above 5.6 mmol/L raises risk of pancreatitis.

Total cholesterol alone is less useful than the full lipid panel and cardiovascular risk score.
A single abnormal result should be repeated fasting to confirm.

Statins (e.g. atorvastatin, rosuvastatin) are the most effective cholesterol-lowering
medications. They are taken once daily, usually in the evening, and work best combined
with a heart-healthy diet: reduce saturated fat, increase fibre, omega-3 foods.
        """.strip(),
    },
    {
        'title': 'Vitamin D Deficiency — Symptoms and Treatment',
        'source_name': 'Terveyskirjasto',
        'source_url': 'https://www.terveyskirjasto.fi',
        'topic': 'vitamins',
        'content': """
Vitamin D is essential for calcium absorption, bone health, immune function, and muscle
strength. Finland's northern latitude means that sunlight-based vitamin D synthesis is
insufficient for most of the year (October–March).

Blood test: 25-hydroxyvitamin D (25-OH D or S-D-25)
- Deficiency: below 30 nmol/L — supplementation strongly recommended
- Insufficiency: 30–50 nmol/L — supplementation recommended, especially in winter
- Adequate: 50–125 nmol/L — optimal range
- Excess: above 150 nmol/L — potentially toxic (very rare)

The Finnish Institute for Health and Welfare (THL) recommends:
- Adults: 10 µg (400 IU) daily, year-round
- Over 60 years: 20 µg (800 IU) daily, year-round
- Children and adolescents: 10 µg daily

Symptoms of deficiency: fatigue, muscle weakness, bone pain, low mood, frequent infections.
Many people with deficiency have no symptoms, which is why routine testing is common.

Vitamin D supplements are safe at recommended doses. Toxicity requires very high doses
(above 100 µg/day for extended periods) — a risk only with unsupervised mega-dosing.

Vitamin D supplements can be taken at any time of day. Fat-soluble, so taking with a
meal containing some fat slightly improves absorption, though the difference is small.
        """.strip(),
    },
    {
        'title': 'Thyroid Function — TSH, T4 and What Your Results Mean',
        'source_name': 'Terveyskirjasto',
        'source_url': 'https://www.terveyskirjasto.fi',
        'topic': 'thyroid',
        'content': """
The thyroid gland produces hormones that regulate metabolism, heart rate, energy levels,
and many other body functions. Thyroid function is assessed through blood tests.

Key thyroid tests:
- TSH (thyroid-stimulating hormone): the most sensitive screening test
  Normal range: approximately 0.4–4.0 mU/L (varies slightly by laboratory)
- Free T4 (thyroxine): the main thyroid hormone in circulation
  Normal range: approximately 9–19 pmol/L
- Free T3 (triiodothyronine): the active form; checked in specific situations

High TSH + low T4 = Hypothyroidism (underactive thyroid)
- Symptoms: fatigue, weight gain, cold intolerance, constipation, slow heart rate, depression
- Treatment: levothyroxine tablet once daily, on an empty stomach, 30–60 minutes before food

Low TSH + high T4/T3 = Hyperthyroidism (overactive thyroid)
- Symptoms: weight loss, rapid heartbeat, sweating, anxiety, heat intolerance, tremor
- Treatment: antithyroid medications, radioiodine, or surgery

Mildly abnormal TSH (e.g. 4–10 mU/L with normal T4) is called subclinical hypothyroidism.
Treatment is individualised — many patients are monitored rather than treated immediately.

Autoimmune thyroid disease is the most common cause in Finland:
- Hashimoto's thyroiditis → hypothyroidism
- Graves' disease → hyperthyroidism
        """.strip(),
    },
    {
        'title': 'Metformin — How It Works and How to Take It',
        'source_name': 'Terveyskirjasto',
        'source_url': 'https://www.terveyskirjasto.fi',
        'topic': 'medications',
        'content': """
Metformin is the most commonly prescribed first-line medication for type 2 diabetes in Finland
and worldwide. It lowers blood glucose primarily by reducing glucose production in the liver.

How to take metformin:
- Always taken WITH food (during or immediately after a meal) to minimise gastrointestinal
  side effects such as nausea, diarrhoea, and stomach cramps.
- Starting low and increasing gradually (e.g. 500 mg once daily, increasing over weeks)
  reduces side effects significantly.
- Standard doses: 500–850 mg two to three times daily, or 1000 mg twice daily.
- Maximum dose: 3000 mg/day in most guidelines.

Extended-release (XR) formulation causes fewer GI side effects and is taken once daily
with the evening meal.

Key safety points:
- Must be temporarily stopped before contrast dye procedures (CT scans with contrast)
  and before any surgery requiring general anaesthesia. Resume 48 hours after.
- Should not be taken if eGFR (kidney function) falls below 30 mL/min.
  Dose reduction is recommended when eGFR is 30–45.
- Alcohol: heavy alcohol use increases the very rare risk of lactic acidosis.

Metformin does NOT cause hypoglycaemia (low blood sugar) when used alone. It is weight-neutral
or may cause modest weight loss. It does not need to be stopped during normal illness unless
vomiting prevents eating.

Annual kidney function check (creatinine/eGFR) is recommended for all patients on metformin.
        """.strip(),
    },
    {
        'title': 'Statins — Cholesterol Medications Explained',
        'source_name': 'Terveyskirjasto',
        'source_url': 'https://www.terveyskirjasto.fi',
        'topic': 'medications',
        'content': """
Statins are medicines that lower LDL ("bad") cholesterol by blocking an enzyme in the liver
(HMG-CoA reductase) used to make cholesterol. They are among the most evidence-based
medicines in cardiovascular prevention.

Commonly prescribed statins in Finland:
- Atorvastatin (Lipitor) — high potency, taken any time of day
- Rosuvastatin (Crestor) — highest potency, taken any time of day
- Simvastatin — moderate potency, taken in the evening (liver makes more cholesterol at night)

When are statins prescribed?
- After a heart attack, stroke, or cardiovascular procedure (always)
- Diabetes with additional risk factors
- High calculated cardiovascular risk (10-year risk above 10%)
- Familial hypercholesterolaemia (inherited high cholesterol)

Common questions:
- When to take: atorvastatin and rosuvastatin can be taken any time. Simvastatin is most
  effective taken in the evening. Taking consistently at the same time improves adherence.
- Side effects: muscle aches are the most reported complaint. Serious muscle breakdown
  (rhabdomyolysis) is very rare. If you have significant muscle pain, contact your doctor.
- Statins are long-term medications — stopping them raises cholesterol back within weeks.
- Grapefruit juice can interact with some statins (especially simvastatin) — best avoided.

Liver enzyme (ALT) monitoring is recommended at baseline. Routine ongoing monitoring is
not needed unless symptoms arise. Statins are generally well-tolerated long-term.
        """.strip(),
    },
    {
        'title': 'Blood Pressure Medications — Common Types and How They Work',
        'source_name': 'Käypä hoito',
        'source_url': 'https://www.kaypahoito.fi',
        'topic': 'medications',
        'content': """
Several classes of medications are used to treat high blood pressure (hypertension).
Many patients need more than one type to reach their blood pressure target.

Main classes used in Finland:

ACE inhibitors (e.g. enalapril, ramipril, lisinopril):
- Block a hormone system that narrows blood vessels
- Also protect kidneys in diabetes — often first choice when diabetes and kidney disease coexist
- Common side effect: dry cough in 10–15% of patients (switch to ARB if troublesome)
- Avoid in pregnancy

ARBs — Angiotensin Receptor Blockers (e.g. losartan, valsartan, candesartan):
- Similar mechanism to ACE inhibitors but without the cough
- Good alternative when ACE inhibitor is not tolerated

Calcium channel blockers (e.g. amlodipine, felodipine):
- Relax blood vessel walls
- Effective for older patients and those of African descent
- Common side effect: ankle swelling

Thiazide diuretics (e.g. hydrochlorothiazide, indapamide):
- Increase salt and water excretion — reduce blood volume
- Often added as a second or third agent

Beta-blockers (e.g. bisoprolol, metoprolol):
- Slow heart rate; mainly used when heart disease is also present
- Not preferred as first-line for hypertension alone in current Finnish guidelines

Blood pressure medication is typically taken once daily, in the morning, unless specifically
advised otherwise. Never stop suddenly without consulting your doctor — rebound hypertension
can occur. Blood pressure usually falls within days of starting treatment.
        """.strip(),
    },
    {
        'title': 'Understanding Lab Reference Ranges',
        'source_name': 'THL',
        'source_url': 'https://thl.fi',
        'topic': 'lab_results',
        'content': """
Reference ranges (also called normal ranges or reference intervals) on a lab report show the
values seen in a healthy reference population, usually the middle 95% of results.

This means that 5% of completely healthy people will have a result outside the reference range
by pure statistical chance. A single result outside the range does not automatically mean disease.

Why results vary between labs:
- Different analysers and reagents give slightly different values
- Reference ranges are calibrated for each lab's local population and equipment
- Always compare your result to the reference range printed on YOUR lab report

Common reasons for abnormal results that are not disease:
- Dehydration (raises creatinine, uric acid, haemoglobin)
- Stress or vigorous exercise before the test (raises cortisol, glucose, creatine kinase)
- Fasting vs. non-fasting (affects glucose, triglycerides)
- Time of day (cortisol and testosterone follow daily rhythms)
- Medications (many medications alter lab values — always report all medications)

What to do with an abnormal result:
1. Check if it is just outside the range (mildly abnormal) or far outside (significantly abnormal)
2. Look for trends — is it getting worse, stable, or improving?
3. Bring the result to your doctor or nurse rather than interpreting it alone
4. Do not panic from a single abnormal value without clinical context

Finnish healthcare: if your result is significantly abnormal, your doctor or nurse
will contact you. You can also review results via your MyKanta patient portal.
        """.strip(),
    },
    {
        'title': 'Type 2 Diabetes — Overview and Management',
        'source_name': 'Käypä hoito',
        'source_url': 'https://www.kaypahoito.fi',
        'topic': 'diabetes',
        'content': """
Type 2 diabetes is a condition where the body does not use insulin effectively, leading to
elevated blood glucose levels over time. It accounts for over 90% of diabetes cases in Finland,
affecting approximately 500,000 Finns.

Diagnosis: HbA1c 48 mmol/mol or above (6.5%), fasting plasma glucose 7.0 mmol/L or above,
or 2-hour glucose 11.1 mmol/L or above after a glucose tolerance test — confirmed on two occasions.

Risk factors: obesity (especially abdominal), physical inactivity, family history,
age above 45, previous gestational diabetes, hypertension, high cholesterol.

Complications if poorly controlled over years:
- Microvascular: kidney disease (diabetic nephropathy), eye disease (retinopathy), nerve damage (neuropathy)
- Macrovascular: heart attack, stroke, peripheral artery disease

Finnish Käypä hoito treatment priorities:
1. Lifestyle: weight loss if overweight, Mediterranean or low-carbohydrate diet, 150+ min/week exercise
2. Blood glucose: target HbA1c below 53 mmol/mol (7%) for most patients; individualised for older patients
3. Blood pressure: target below 130/80 mmHg
4. Cholesterol: statin therapy for most patients with type 2 diabetes
5. Regular monitoring: HbA1c every 6–12 months, kidney function annually, eye screening every 1–3 years, foot check annually

Modern medications (in addition to metformin):
- SGLT2 inhibitors (empagliflozin, dapagliflozin): lower glucose and protect heart and kidneys
- GLP-1 receptor agonists (semaglutide, liraglutide): lower glucose, cause weight loss, protect heart
        """.strip(),
    },
    {
        'title': 'Sleep and Health — What Your Sleep Patterns Tell You',
        'source_name': 'THL',
        'source_url': 'https://thl.fi',
        'topic': 'wearable',
        'content': """
Sleep is essential for physical recovery, memory consolidation, immune function, and metabolic
health. Adults need 7–9 hours of sleep per night; quality matters as much as quantity.

Sleep stages measured by wearables and sleep studies:
- Light sleep (N1, N2): transition and light sleep, easier to wake
- Deep sleep (N3, slow-wave): physical restoration, immune support, glucose regulation
- REM sleep: memory consolidation, emotional processing, dreaming

Common sleep problems and their significance:
- Difficulty falling asleep (sleep onset insomnia): often linked to stress, anxiety, caffeine, screens
- Frequent waking (maintenance insomnia): may relate to sleep apnoea, pain, or mood disorders
- Early waking: sometimes a sign of depression or anxiety
- Poor sleep quality despite adequate hours: fragmented sleep from sleep apnoea, restless legs

Sleep and metabolic health:
- Short sleep duration (below 6 hours) is associated with higher risk of type 2 diabetes,
  hypertension, and obesity — even over months, not just occasionally
- Poor sleep raises cortisol and ghrelin (hunger hormone), which worsens glucose control
  and promotes weight gain

Practical sleep hygiene:
- Consistent sleep and wake times, even on weekends
- Bedroom dark, cool (16–19°C), and quiet
- Avoid screens (blue light) 60 minutes before bed
- Avoid caffeine after 14:00
- Limit alcohol — it reduces REM sleep quality despite helping you fall asleep faster

If you snore loudly, wake unrefreshed, or your partner reports breathing pauses, discuss
sleep apnoea screening with your doctor. It is very treatable with CPAP therapy.
        """.strip(),
    },
]


class Command(BaseCommand):
    help = 'Seed the general knowledge base with curated Finnish health articles'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear', action='store_true',
            help='Delete all existing GeneralKnowledgeChunk rows before indexing',
        )

    def handle(self, *args, **options):
        from apps.rag_assistant.models import GeneralKnowledgeChunk
        from apps.rag_assistant.services.general_knowledge_service import GeneralKnowledgeService

        if options['clear']:
            deleted, _ = GeneralKnowledgeChunk.objects.all().delete()
            self.stdout.write(f'Cleared {deleted} existing chunks.')

        svc = GeneralKnowledgeService()
        self.stdout.write(f'Indexing {len(ARTICLES)} articles...')

        count = svc.index_articles(ARTICLES)
        self.stdout.write(self.style.SUCCESS(
            f'Done. {count} chunks indexed from {len(ARTICLES)} articles.'
        ))
