// ==========================================
// Bulk Student Import Service (Native CSV / Excel RFC 4180)
// Parsing, header mapping, pre-validation and the error-report CSV.
// Persistence now goes through the server endpoint POST /students/bulk-import
// (see StudentsPage / ClassesPage); the legacy localStorage commitImport was
// retired so imports actually persist to the database.
// Developed by NTS Novalytix
// ==========================================

import { ImportRowError } from '../types';
import { studentService } from './studentService';
import { classService } from './classService';

export interface ParsedImportRow {
  rowNumber: number;
  admissionNo?: string;
  firstName: string;
  lastName: string;
  gender: 'Male' | 'Female' | 'Other';
  dob: string;
  bloodGroup?: string;
  grade: number;
  section: string;
  rollNo?: string;
  parentName: string;
  parentPhone: string;
  parentEmail?: string;
  address?: string;
  targetClassId?: string;
}

export const importService = {
  /**
   * Generates a sample CSV template with standard headers and sample rows
   */
  generateSampleCSV: (): string => {
    const headers = [
      'Admission No',
      'First Name',
      'Last Name',
      'Gender',
      'Date of Birth',
      'Blood Group',
      'Grade',
      'Section',
      'Roll No',
      'Parent Name',
      'Parent Phone',
      'Parent Email',
      'Address',
    ].join(',');

    const sampleRow1 = [
      'ADM-2026-901',
      'Vikrant',
      'Deshmukh',
      'Male',
      '2010-05-18',
      'O+',
      '10',
      'A',
      '10A-39',
      'Anand Deshmukh',
      '+91 98333 44556',
      'anand.deshmukh@gmail.com',
      '"Flat 402, Sunshine Heights, Bengaluru"',
    ].join(',');

    const sampleRow2 = [
      'ADM-2026-902',
      'Meera',
      'Iyer',
      'Female',
      '2011-03-22',
      'A+',
      '9',
      'A',
      '9A-36',
      'Ramanathan Iyer',
      '+91 98444 77889',
      'iyer.family@yahoo.com',
      '"Villa 12, Green Meadows, Bengaluru"',
    ].join(',');

    return `${headers}\n${sampleRow1}\n${sampleRow2}\n`;
  },

  /**
   * Native RFC 4180 compliant CSV parser
   * Handles quoted strings, escaped quotes (""), commas inside quotes, and newlines
   */
  parseCSV: (csvText: string): { headers: string[]; rows: Record<string, string>[] } => {
    const lines: string[][] = [];
    let currentRow: string[] = [];
    let currentField = '';
    let insideQuotes = false;

    // Normalize CRLF to LF
    const cleanText = csvText.replace(/\r\n/g, '\n').replace(/\r/g, '\n');

    for (let i = 0; i < cleanText.length; i++) {
      const char = cleanText[i];
      const nextChar = cleanText[i + 1];

      if (char === '"') {
        if (insideQuotes && nextChar === '"') {
          currentField += '"';
          i++; // Skip escaped quote
        } else {
          insideQuotes = !insideQuotes;
        }
      } else if (char === ',' && !insideQuotes) {
        currentRow.push(currentField.trim());
        currentField = '';
      } else if (char === '\n' && !insideQuotes) {
        currentRow.push(currentField.trim());
        if (currentRow.some(f => f.length > 0)) {
          lines.push(currentRow);
        }
        currentRow = [];
        currentField = '';
      } else {
        currentField += char;
      }
    }

    if (currentField.length > 0 || currentRow.length > 0) {
      currentRow.push(currentField.trim());
      if (currentRow.some(f => f.length > 0)) {
        lines.push(currentRow);
      }
    }

    if (lines.length === 0) {
      return { headers: [], rows: [] };
    }

    const headers = lines[0].map(h => h.replace(/^["']|["']$/g, '').trim());
    const rows: Record<string, string>[] = [];

    for (let r = 1; r < lines.length; r++) {
      const rowData = lines[r];
      const rowObj: Record<string, string> = {};
      headers.forEach((h, idx) => {
        rowObj[h] = rowData[idx] !== undefined ? rowData[idx].replace(/^["']|["']$/g, '').trim() : '';
      });
      rows.push(rowObj);
    }

    return { headers, rows };
  },

  /**
   * Maps dynamic header labels to standardized ERP student fields
   */
  mapRowToStudent: (rawRow: Record<string, string>, rowNumber: number): ParsedImportRow => {
    const getValue = (possibleHeaders: string[]): string => {
      for (const h of possibleHeaders) {
        for (const [key, val] of Object.entries(rawRow)) {
          if (key.toLowerCase().replace(/[^a-z0-9]/g, '') === h.toLowerCase().replace(/[^a-z0-9]/g, '')) {
            return val || '';
          }
        }
      }
      return '';
    };

    const rawGender = getValue(['gender', 'sex']).toLowerCase();
    let gender: 'Male' | 'Female' | 'Other' = 'Other';
    if (rawGender.startsWith('m')) gender = 'Male';
    else if (rawGender.startsWith('f')) gender = 'Female';

    const fullNameVal = getValue(['fullname', 'name', 'studentname']);
    let firstName = getValue(['firstname', 'first_name', 'fname', 'givenname']);
    let lastName = getValue(['lastname', 'last_name', 'lname', 'surname']);
    if ((!firstName || !lastName) && fullNameVal) {
      const parts = fullNameVal.trim().split(' ');
      if (!firstName) firstName = parts[0] || '';
      if (!lastName) lastName = parts.slice(1).join(' ') || '';
    }

    const gradeStr = getValue(['grade', 'class', 'classgrade', 'standard']).replace(/[^0-9]/g, '');

    return {
      rowNumber,
      admissionNo: getValue(['admissionno', 'admissionnumber', 'admno', 'enrollmentno']),
      firstName,
      lastName,
      gender,
      dob: getValue(['dateofbirth', 'dob', 'birthdate']),
      bloodGroup: getValue(['bloodgroup', 'blood_group', 'bloodgrp']) || 'O+',
      grade: Number(gradeStr) || 10,
      section: getValue(['section', 'sec']).toUpperCase() || 'A',
      rollNo: getValue(['rollno', 'roll_no', 'rollnumber']),
      parentName: getValue(['parentname', 'guardianname', 'fathername', 'parent_name']),
      parentPhone: getValue(['parentphone', 'guardianphone', 'contactnumber', 'phone', 'mobile']),
      parentEmail: getValue(['parentemail', 'guardianemail', 'email']),
      address: getValue(['address', 'residentialaddress', 'location']),
    };
  },

  /**
   * Pre-validates all parsed rows before any storage write
   */
  validateImportRows: (
    rows: ParsedImportRow[],
    sessionId: string
  ): {
    validRows: ParsedImportRow[];
    invalidRows: ParsedImportRow[];
    errors: ImportRowError[];
    summary: {
      total: number;
      valid: number;
      invalid: number;
      duplicateAdmissionNos: number;
      missingRequired: number;
      invalidClasses: number;
    };
  } => {
    const existingStudents = studentService.getAll();
    const classes = classService.getBySession(sessionId);

    const validRows: ParsedImportRow[] = [];
    const invalidRows: ParsedImportRow[] = [];
    const errors: ImportRowError[] = [];

    const seenAdmissionNosInFile = new Set<string>();
    let duplicateAdmissionNos = 0;
    let missingRequired = 0;
    let invalidClasses = 0;

    rows.forEach(row => {
      const rowErrors: string[] = [];

      // Required: First Name & Last Name
      if (!row.firstName || row.firstName.length < 2) {
        rowErrors.push('Full Name is required (first name min 2 chars).');
        missingRequired++;
      }
      if (!row.lastName) {
        rowErrors.push('Full Name is required (last name missing).');
        missingRequired++;
      }

      // Required: DOB
      if (!row.dob || isNaN(Date.parse(row.dob))) {
        rowErrors.push('Valid Date of Birth is required (YYYY-MM-DD).');
        missingRequired++;
      }

      // Required: Parent Name & Phone
      if (!row.parentName) {
        rowErrors.push('Parent / Guardian name is required.');
        missingRequired++;
      }
      if (!row.parentPhone || row.parentPhone.replace(/\D/g, '').length < 8) {
        rowErrors.push('Valid parent contact phone number is required.');
        missingRequired++;
      }

      // Admission number uniqueness if provided
      if (row.admissionNo) {
        const cleanAdm = row.admissionNo.toLowerCase().trim();
        if (seenAdmissionNosInFile.has(cleanAdm)) {
          rowErrors.push(`Duplicate admission number "${row.admissionNo}" already exists within uploaded file.`);
          duplicateAdmissionNos++;
        } else {
          seenAdmissionNosInFile.add(cleanAdm);
        }

        const existingAdm = existingStudents.find(
          s => s.admissionNo.toLowerCase().trim() === cleanAdm
        );
        if (existingAdm) {
          rowErrors.push(`Admission number "${row.admissionNo}" already exists and is registered to ${existingAdm.fullName}.`);
          duplicateAdmissionNos++;
        }
      }

      // Duplicate student risk (same name + dob/phone)
      const cleanFirst = row.firstName.toLowerCase().trim();
      const cleanLast = row.lastName.toLowerCase().trim();
      const cleanPhone = (row.parentPhone || '').replace(/\D/g, '');

      const dupIdentity = existingStudents.find(
        s =>
          s.firstName.toLowerCase() === cleanFirst &&
          s.lastName.toLowerCase() === cleanLast &&
          (s.dob === row.dob || s.parentPhone.replace(/\D/g, '') === cleanPhone)
      );
      if (dupIdentity) {
        rowErrors.push(`Possible duplicate student: Already enrolled as ${dupIdentity.fullName} (${dupIdentity.admissionNo}).`);
      }

      // Validate target class in this session
      const matchedClass = classes.find(
        c => c.grade === row.grade && c.section.toUpperCase() === row.section.toUpperCase()
      );

      if (!matchedClass) {
        rowErrors.push(`Target class not found: No active class exists for Grade ${row.grade} Section ${row.section} in session.`);
        invalidClasses++;
      } else {
        row.targetClassId = matchedClass.id;
      }

      if (rowErrors.length > 0) {
        rowErrors.forEach(msg => {
          errors.push({ row: row.rowNumber, message: msg });
        });
        invalidRows.push(row);
      } else {
        validRows.push(row);
      }
    });

    return {
      validRows,
      invalidRows,
      errors,
      summary: {
        total: rows.length,
        valid: validRows.length,
        invalid: invalidRows.length,
        duplicateAdmissionNos,
        missingRequired,
        invalidClasses,
      },
    };
  },

  /**
   * Helper that parses raw CSV string and validates all rows against the given session
   */
  parseAndValidateCSV: (
    csvText: string,
    sessionId: string
  ): {
    totalRows: number;
    validRows: ParsedImportRow[];
    invalidRows: ParsedImportRow[];
    errorRows: { rowNumber: number; errors: string[] }[];
    errors: ImportRowError[];
    summary: {
      total: number;
      valid: number;
      invalid: number;
      duplicateAdmissionNos: number;
      missingRequired: number;
      invalidClasses: number;
    };
  } => {
    const { rows } = importService.parseCSV(csvText);
    const parsedRows = rows.map((r, idx) => importService.mapRowToStudent(r, idx + 2));
    const validated = importService.validateImportRows(parsedRows, sessionId);

    // Group errors by rowNumber for easy inspection
    const errorMap = new Map<number, string[]>();
    validated.errors.forEach(err => {
      const existing = errorMap.get(err.row) || [];
      existing.push(err.message);
      errorMap.set(err.row, existing);
    });

    const errorRows = Array.from(errorMap.entries()).map(([rowNumber, errs]) => ({
      rowNumber,
      errors: errs,
    }));

    return {
      totalRows: rows.length,
      validRows: validated.validRows,
      invalidRows: validated.invalidRows,
      errorRows,
      errors: validated.errors,
      summary: validated.summary,
    };
  },

  /**
   * Generates downloadable error report CSV
   */
  generateErrorReportCSV: (errors: ImportRowError[]): string => {
    const headers = 'Row Number,Field,Error Description\n';
    const lines = errors.map(e => `${e.row},"${e.field || 'General'}","${e.message.replace(/"/g, '""')}"`);
    return headers + lines.join('\n');
  },
};
