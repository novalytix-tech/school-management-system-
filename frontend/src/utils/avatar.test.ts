import { describe, it, expect } from 'vitest';
import { resolveUserAvatar, withoutBorrowedDemoPhoto, personaForAccount } from './avatar';
import { DEMO_USERS } from '../data/seedData';

describe('withoutBorrowedDemoPhoto', () => {
  it('drops a demo persona photo from a different (real) account', () => {
    const teacherPersona = DEMO_USERS.teacher;
    const realTeacher = { id: 'dee2e069-547d-4338-8e41-3d6636076050', name: 'Ananya Sharma', photoUrl: teacherPersona.photoUrl };
    expect(withoutBorrowedDemoPhoto(realTeacher).photoUrl).toBeUndefined();
  });

  it('keeps the persona\'s own photo and real uploaded photos', () => {
    expect(withoutBorrowedDemoPhoto(DEMO_USERS.teacher).photoUrl).toBe(DEMO_USERS.teacher.photoUrl);
    const uploaded = { id: 'u-1', photoUrl: 'https://cdn.example.com/u-1.jpg' };
    expect(withoutBorrowedDemoPhoto(uploaded).photoUrl).toBe(uploaded.photoUrl);
  });
});

describe('resolveUserAvatar without a stored photo', () => {
  it('falls back to the role portrait instead of another persona', () => {
    expect(resolveUserAvatar('school_admin', { id: 'x', name: 'Arvind Swaminathan' })).toBe('/admin-profile.svg');
    expect(resolveUserAvatar('parent', { id: 'y', name: 'Vikram Patel' })).toBe('/parent-profile.svg');
  });
});

describe('persona accounts', () => {
  it('maps only the quick-login accounts to their persona', () => {
    expect(personaForAccount('principal@stxaviers.edu.in', 'school_admin')?.name).toBe('Ritesh Singh');
    expect(personaForAccount('ANANYA.SHARMA@stxaviers.edu.in', 'teacher')?.photoUrl).toBe('/sharad-teacher.svg');
    expect(personaForAccount('vikram.patel@gmail.com', 'parent')?.name).toBe('Shakti Singh');
    expect(personaForAccount('shivam.giri@student.stxaviers.edu.in', 'student')?.photoUrl).toBe('/shivam-giri.svg');
    // A different real student is never shown with Shivam's photo.
    expect(personaForAccount('aarav.sharma@student.stxaviers.edu.in', 'student')).toBeUndefined();
    // Wrong role for the account: not the persona.
    expect(personaForAccount('principal@stxaviers.edu.in', 'teacher')).toBeUndefined();
  });

  it('keeps a persona photo on its own account but strips it elsewhere', () => {
    const ritesh = { id: 'uuid-1', email: 'principal@stxaviers.edu.in', role: 'school_admin' as const, photoUrl: '/ritesh-admin.svg' };
    expect(withoutBorrowedDemoPhoto(ritesh).photoUrl).toBe('/ritesh-admin.svg');
    const other = { id: 'uuid-2', email: 'someone@stxaviers.edu.in', role: 'school_admin' as const, photoUrl: '/ritesh-admin.svg' };
    expect(withoutBorrowedDemoPhoto(other).photoUrl).toBeUndefined();
  });
});
