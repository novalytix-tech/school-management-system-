import { describe, it, expect } from 'vitest';
import { resolveUserAvatar, withoutBorrowedDemoPhoto } from './avatar';
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
