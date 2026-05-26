import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "/Users/ikedashinji/Desktop/holon_workspace/maigent_3/document.xlsx";
const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);
const sheetNames = workbook.worksheets.items.map((sheet) => sheet.name);

for (const sheetName of sheetNames) {
  const result = await workbook.inspect({
    kind: "table",
    range: `${sheetName}!A1:K80`,
    include: "values,formulas",
    tableMaxRows: 80,
    tableMaxCols: 11,
  });
  console.log(`\\n===== ${sheetName} =====`);
  console.log(result.ndjson);
}
