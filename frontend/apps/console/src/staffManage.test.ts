import { describe, expect, it } from 'vitest'

import { manageAccess, manageHint } from './staffManage'

const me = { id: 'me', permissions: new Set(['staff:read', 'staff:manage', 'customer:read']) }

describe('manageAccess', () => {
  it('marks your own card', () => {
    expect(manageAccess({ id: 'me', permissions: [] }, me)).toBe('self')
  })

  it('allows staff whose permissions you all have, whatever their role', () => {
    expect(manageAccess({ id: 'a', permissions: ['customer:read'] }, me)).toBe('ok')
    expect(manageAccess({ id: 'b', permissions: [] }, me)).toBe('ok')
  })

  it('refuses staff with a permission you lack', () => {
    expect(manageAccess({ id: 'c', permissions: ['customer:read', 'order:review'] }, me)).toBe('higher')
  })
})

describe('manageHint', () => {
  it('says why a greyed-out button cannot be used', () => {
    expect(manageHint('higher', '重置')).toBe('权限高于你，请让管理员重置')
    expect(manageHint('higher', '停用')).toBe('权限高于你，请让管理员停用')
    expect(manageHint('higher', '删除')).toBe('权限高于你，请让管理员删除')
    expect(manageHint('self', '删除')).toBe('不能删除自己的账号')
  })

  it('has nothing to say when the button can be used', () => {
    expect(manageHint('ok', '删除')).toBeNull()
  })
})
