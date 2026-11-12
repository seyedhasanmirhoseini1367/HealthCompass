function toggleSchemaField() {
  const t = document.getElementById('inputTypeSelect').value;
  const s = document.getElementById('schemaGroup');
  s.style.display = t === 'tabular' ? 'block' : 'none';
}
toggleSchemaField();
