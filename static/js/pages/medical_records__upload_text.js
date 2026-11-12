function updateCount() {
  document.getElementById('char-count').textContent =
    document.getElementById('id_text').value.length;
}
updateCount();

document.getElementById('paste-form').addEventListener('submit', function() {
  var btn = document.getElementById('submit-btn');
  btn.disabled = true;
  btn.innerHTML = hcIcon('spinner', 'icon-spin') + ' AI is parsing…';
});

var EXAMPLES = {
  lab: `Patient: Demo Patient\nDate: 2024-03-15\n\nBlood Test Results:\nHemoglobin: 142 g/L (ref 130-175)\nGlucose (fasting): 6.1 mmol/L (ref 3.9-5.6)  HIGH\nHbA1c: 42 mmol/mol (ref <48)\nTotal Cholesterol: 5.4 mmol/L (ref <5.0)  HIGH\nLDL: 3.2 mmol/L (ref <3.0)  HIGH\nHDL: 1.4 mmol/L (ref >1.0)\nTriglycerides: 1.8 mmol/L (ref <1.7)  HIGH\nCreatinine: 82 umol/L (ref 60-110)\nSodium: 140 mmol/L (ref 136-145)\nPotassium: 4.1 mmol/L (ref 3.5-5.0)\n\nOrdering physician: Dr. A. Mäkinen\nFacility: Terveystalo Helsinki`,

  rx: `PRESCRIPTION\nDate: 2024-03-15\nPatient: Demo Patient, DOB 1979-06-20\n\nMedication 1:\nMetformin 500mg\nTake 1 tablet twice daily with meals\nQty: 60 tablets, 2 refills\n\nMedication 2:\nAtorvastatin 20mg\nTake 1 tablet at bedtime\nQty: 30 tablets, 3 refills\n\nPrescribing physician: Dr. A. Mäkinen\nLicense: FI-12345`,

  note: `OUTPATIENT VISIT NOTE\nDate: 2024-03-15\nPatient: Demo Patient, 45M\n\nChief Complaint: Routine annual check-up. Patient reports fatigue and mild shortness of breath on exertion for the past 2 months.\n\nAssessment:\n1. Type 2 Diabetes Mellitus (E11.9) — HbA1c mildly elevated at 42 mmol/mol. Continue Metformin, dietary counselling advised.\n2. Hyperlipidaemia (E78.5) — LDL elevated. Starting Atorvastatin 20mg.\n3. Essential Hypertension (I10) — BP 138/88 today. Lifestyle modification advised.\n\nPlan:\n- Repeat labs in 3 months\n- Refer to dietitian\n- Follow-up in 6 weeks\n\nDr. A. Mäkinen, MD`
};

function fillExample(type) {
  var ta = document.getElementById('id_text');
  ta.value = EXAMPLES[type];
  updateCount();
  ta.focus();
}
