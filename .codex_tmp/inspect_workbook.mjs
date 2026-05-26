import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "/Users/ikedashinji/Desktop/holon_workspace/maigent_3/document.xlsx";
const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);

const summary = {
  workbookKeys: Object.keys(workbook),
  worksheetsKeys: Object.keys(workbook.worksheets ?? {}),
};

try {
  summary.sheetNames = workbook.worksheets.items?.map((sheet) => sheet.name);
} catch (error) {
  summary.sheetNamesError = String(error);
}

try {
  summary.sheetNames2 = workbook.worksheets.toArray?.().map((sheet) => sheet.name);
} catch (error) {
  summary.sheetNames2Error = String(error);
}

console.log(JSON.stringify(summary, null, 2));
