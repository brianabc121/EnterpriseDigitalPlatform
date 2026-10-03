import { describe, expect, it } from 'vitest'

import { assignableRoles, cardActions, manageAccess, manageHint, receivesHandover, showsHandover } from './staffManage'

const me = { id: 'me', permissions: new Set(['staff:read', 'staff:manage', 'customer:read']) }

describe('manageAccess', () => {
  it('marks your own card', () => {
    expect(manageAccess({ id: 'me', permissions: [] }, me)).toBe('self')
    expect(manageAccess({ id: 'me', permissions: [], is_owner: true }, me)).toBe('self')
  })

  it('allows staff whose permissions you all have, whatever their role', () => {
    expect(manageAccess({ id: 'a', permissions: ['customer:read'] }, me)).toBe('ok')
    expect(manageAccess({ id: 'b', permissions: [] }, me)).toBe('ok')
  })

  it('refuses staff with a permission you lack', () => {
    expect(manageAccess({ id: 'c', permissions: ['customer:read', 'order:review'] }, me)).toBe('higher')
  })

  it('leaves the enterprise owner to the owner and the platform, even with all permissions', () => {
    expect(manageAccess({ id: 'o', permissions: [], is_owner: true }, me)).toBe('owner')
  })
})

describe('manageHint', () => {
  it('says why a greyed-out button cannot be used', () => {
    expect(manageHint('higher', '重置')).toBe('权限高于你，请让管理员重置')
    expect(manageHint('higher', '停用')).toBe('权限高于你，请让管理员停用')
    expect(manageHint('higher', '删除')).toBe('权限高于你，请让管理员删除')
    expect(manageHint('self', '删除')).toBe('不能删除自己的账号')
  })

  it('explains the owner card', () => {
    expect(manageHint('owner', '重置')).toBe('企业所有者的密码由本人修改，或由平台运维人员重置')
    expect(manageHint('owner', '编辑')).toBe('企业所有者的资料只能由本人修改')
    expect(manageHint('owner', '停用')).toBe('不能停用企业所有者')
  })

  it('has nothing to say when the button can be used', () => {
    expect(manageHint('ok', '删除')).toBeNull()
  })
})

describe('assignableRoles', () => {
  it('never offers the enterprise owner role', () => {
    const roles = [{ code: 'tenant_admin' }, { code: 'agent' }, { code: 'finance' }]
    expect(assignableRoles(roles).map((r) => r.code)).toEqual(['agent', 'finance'])
  })
})

describe('showsHandover', () => {
  // 角色的岗位（§25.15）：系统角色固定，自定义角色可以选择。
  const consoles = new Map([
    ['tenant_admin', 'admin'],
    ['agent', 'agent'],
    ['supervisor', 'supervisor'],
    ['finance', 'finance'],
    ['presales', 'agent'],
  ])

  it('shows 交接客户 only on the cards of 客服 positions, including custom roles in that position', () => {
    expect(showsHandover({ roles: ['agent'] }, consoles)).toBe(true)
    expect(showsHandover({ roles: ['presales'] }, consoles)).toBe(true)
    expect(showsHandover({ roles: ['finance', 'agent'] }, consoles)).toBe(true)
    for (const roles of [['tenant_admin'], ['supervisor'], ['finance'], [], ['unknown']]) {
      expect(showsHandover({ roles }, consoles)).toBe(false)
    }
  })
})

describe('receivesHandover', () => {
  const consoles = new Map([
    ['tenant_admin', 'admin'],
    ['agent', 'agent'],
    ['supervisor', 'supervisor'],
    ['finance', 'finance'],
    ['deputy', 'admin'],
    ['hr', 'supervisor'],
  ])
  const desk = ['workbench:use', 'customer:read']

  it('takes 客服, 主管 and the enterprise owner', () => {
    expect(receivesHandover({ roles: ['agent'], permissions: desk }, consoles)).toBe(true)
    expect(receivesHandover({ roles: ['supervisor'], permissions: desk }, consoles)).toBe(true)
    expect(receivesHandover({ roles: ['tenant_admin'], permissions: [] }, consoles)).toBe(true)
  })

  it('leaves out other positions and roles that cannot serve customers', () => {
    expect(receivesHandover({ roles: ['finance'], permissions: ['finance:view'] }, consoles)).toBe(false)
    expect(receivesHandover({ roles: ['deputy'], permissions: desk }, consoles)).toBe(false)
    // "人事"按权限判断岗位是主管，但没有工作台。
    expect(receivesHandover({ roles: ['hr'], permissions: ['staff:read', 'staff:manage'] }, consoles)).toBe(false)
  })
})

describe('cardActions', () => {
  const consoles = new Map([
    ['tenant_admin', 'admin'],
    ['agent', 'agent'],
    ['finance', 'finance'],
    ['hr', 'supervisor'],
  ])
  const all = ['staff:read', 'staff:manage', 'customer:assign', 'workbench:use', 'customer:read', 'finance:view']
  const owner = { id: 'boss', permissions: new Set(all), canManage: true, canHandover: true }
  const hr = { id: 'hrm', permissions: new Set(['staff:read', 'staff:manage']), canManage: true, canHandover: false }
  const card = (id: string, roles: string[], permissions: string[], extra: object = {}) => ({
    id,
    status: 'active',
    roles,
    permissions,
    ...extra,
  })
  const summary = (actions: ReturnType<typeof cardActions>) =>
    actions.map((a) => (a.hint ? `${a.label}（${a.hint}）` : a.label))

  it('puts every action of a card in the menu', () => {
    const agent = card('mei', ['agent'], ['workbench:use', 'customer:read'])
    expect(summary(cardActions(agent, owner, consoles))).toEqual(['编辑资料', '重置密码', '停用', '交接客户'])
    expect(cardActions(agent, owner, consoles).find((a) => a.key === 'toggle')?.danger).toBe(true)
    const disabled = card('fay', ['finance'], ['finance:view'], { status: 'disabled' })
    expect(summary(cardActions(disabled, owner, consoles))).toEqual(['编辑资料', '重置密码', '启用'])
  })

  it('offers 修改密码 on your own card and nothing to disable', () => {
    const self = card('boss', ['tenant_admin'], all, { is_owner: true })
    expect(summary(cardActions(self, owner, consoles))).toEqual(['编辑资料', '修改密码'])
  })

  it('greys out what you may not do and says why', () => {
    const self = card('boss', ['tenant_admin'], all, { is_owner: true })
    expect(summary(cardActions(self, hr, consoles))).toEqual([
      '编辑资料（企业所有者的资料只能由本人修改）',
      '重置密码（企业所有者的密码由本人修改，或由平台运维人员重置）',
    ])
    const agent = card('mei', ['agent'], ['workbench:use', 'customer:read'])
    expect(summary(cardActions(agent, hr, consoles))).toEqual([
      '编辑资料（权限高于你，请让管理员编辑）',
      '重置密码（权限高于你，请让管理员重置）',
      '停用（权限高于你，请让管理员停用）',
    ])
  })

  it('has nothing for viewers who can neither manage staff nor hand over customers', () => {
    const viewer = { id: 'vic', permissions: new Set(['staff:read']), canManage: false, canHandover: false }
    expect(cardActions(card('mei', ['agent'], []), viewer, consoles)).toEqual([])
  })
})
