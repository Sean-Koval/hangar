"""Hidden grader for the semver harness eval. Expected values come from node-semver 7.8.5."""
import os, subprocess, sys, unittest
sys.path.insert(0, os.getcwd())
import semver as S

HERE = os.getcwd()

class Hidden(unittest.TestCase):
    pass

def add(name, fn):
    setattr(Hidden, name, fn)

def _t1(self):
    S.Version.parse('1.2.3')
add("test_parse_valid__001", _t1)

def _t2(self):
    S.Version.parse('v1.2.3')
add("test_parse_valid__002", _t2)

def _t3(self):
    S.Version.parse('1.2.3-alpha')
add("test_parse_valid__003", _t3)

def _t4(self):
    S.Version.parse('1.2.3-0a')
add("test_parse_valid__004", _t4)

def _t5(self):
    S.Version.parse('1.2.3+001')
add("test_parse_valid__005", _t5)

def _t6(self):
    S.Version.parse('1.2.3-alpha.1+build.7')
add("test_parse_valid__006", _t6)

def _t7(self):
    with self.assertRaises(ValueError):
        S.Version.parse('01.2.3')
add("test_parse_invalid__007", _t7)

def _t8(self):
    with self.assertRaises(ValueError):
        S.Version.parse('1.2')
add("test_parse_invalid__008", _t8)

def _t9(self):
    with self.assertRaises(ValueError):
        S.Version.parse('1.2.3-')
add("test_parse_invalid__009", _t9)

def _t10(self):
    with self.assertRaises(ValueError):
        S.Version.parse('1.2.3+')
add("test_parse_invalid__010", _t10)

def _t11(self):
    with self.assertRaises(ValueError):
        S.Version.parse('1.2.3-01')
add("test_parse_invalid__011", _t11)

def _t12(self):
    with self.assertRaises(ValueError):
        S.Version.parse('1.2.3-a..b')
add("test_parse_invalid__012", _t12)

def _t13(self):
    with self.assertRaises(ValueError):
        S.Version.parse('1.2.3.4')
add("test_parse_invalid__013", _t13)

def _t14(self):
    with self.assertRaises(ValueError):
        S.Version.parse('a.b.c')
add("test_parse_invalid__014", _t14)

def _t15(self):
    with self.assertRaises(ValueError):
        S.Version.parse('1.2.3-alpha_1')
add("test_parse_invalid__015", _t15)

def _t16(self):
    v = S.Version.parse('v1.2.3-alpha.1+build.7')
    self.assertEqual((v.major, v.minor, v.patch), (1, 2, 3))
    self.assertEqual(tuple(str(x) for x in v.prerelease), ('alpha', '1'))
    self.assertEqual(tuple(v.build), ('build', '7'))
    self.assertEqual(str(v), '1.2.3-alpha.1+build.7')
add("test_parse_fields__016", _t16)

def _t17(self):
    vs = [S.Version.parse(x) for x in ['1.0.0', '1.0.0-rc.1', '1.0.0-beta.11', '1.0.0-alpha.beta', '1.0.0-beta', '1.0.0-alpha', '1.0.0-beta.2', '1.0.0-alpha.1', '0.9.9', '10.0.0', '2.0.0', '1.10.0', '1.2.0']]
    self.assertEqual([str(x) for x in sorted(vs)], ['0.9.9', '1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-alpha.beta', '1.0.0-beta', '1.0.0-beta.2', '1.0.0-beta.11', '1.0.0-rc.1', '1.0.0', '1.2.0', '1.10.0', '2.0.0', '10.0.0'])
add("test_ordering__017", _t17)

def _t18(self):
    self.assertLess(S.Version.parse('0.9.9'), S.Version.parse('1.0.0-alpha'))
    self.assertGreater(S.Version.parse('1.0.0-alpha'), S.Version.parse('0.9.9'))
add("test_ordering__018", _t18)

def _t19(self):
    self.assertLess(S.Version.parse('1.0.0-alpha'), S.Version.parse('1.0.0-alpha.1'))
    self.assertGreater(S.Version.parse('1.0.0-alpha.1'), S.Version.parse('1.0.0-alpha'))
add("test_ordering__019", _t19)

def _t20(self):
    self.assertLess(S.Version.parse('1.0.0-alpha.1'), S.Version.parse('1.0.0-alpha.beta'))
    self.assertGreater(S.Version.parse('1.0.0-alpha.beta'), S.Version.parse('1.0.0-alpha.1'))
add("test_ordering__020", _t20)

def _t21(self):
    self.assertLess(S.Version.parse('1.0.0-alpha.beta'), S.Version.parse('1.0.0-beta'))
    self.assertGreater(S.Version.parse('1.0.0-beta'), S.Version.parse('1.0.0-alpha.beta'))
add("test_ordering__021", _t21)

def _t22(self):
    self.assertLess(S.Version.parse('1.0.0-beta'), S.Version.parse('1.0.0-beta.2'))
    self.assertGreater(S.Version.parse('1.0.0-beta.2'), S.Version.parse('1.0.0-beta'))
add("test_ordering__022", _t22)

def _t23(self):
    self.assertLess(S.Version.parse('1.0.0-beta.2'), S.Version.parse('1.0.0-beta.11'))
    self.assertGreater(S.Version.parse('1.0.0-beta.11'), S.Version.parse('1.0.0-beta.2'))
add("test_ordering__023", _t23)

def _t24(self):
    self.assertLess(S.Version.parse('1.0.0-beta.11'), S.Version.parse('1.0.0-rc.1'))
    self.assertGreater(S.Version.parse('1.0.0-rc.1'), S.Version.parse('1.0.0-beta.11'))
add("test_ordering__024", _t24)

def _t25(self):
    self.assertLess(S.Version.parse('1.0.0-rc.1'), S.Version.parse('1.0.0'))
    self.assertGreater(S.Version.parse('1.0.0'), S.Version.parse('1.0.0-rc.1'))
add("test_ordering__025", _t25)

def _t26(self):
    self.assertLess(S.Version.parse('1.0.0'), S.Version.parse('1.2.0'))
    self.assertGreater(S.Version.parse('1.2.0'), S.Version.parse('1.0.0'))
add("test_ordering__026", _t26)

def _t27(self):
    self.assertLess(S.Version.parse('1.2.0'), S.Version.parse('1.10.0'))
    self.assertGreater(S.Version.parse('1.10.0'), S.Version.parse('1.2.0'))
add("test_ordering__027", _t27)

def _t28(self):
    self.assertLess(S.Version.parse('1.10.0'), S.Version.parse('2.0.0'))
    self.assertGreater(S.Version.parse('2.0.0'), S.Version.parse('1.10.0'))
add("test_ordering__028", _t28)

def _t29(self):
    self.assertLess(S.Version.parse('2.0.0'), S.Version.parse('10.0.0'))
    self.assertGreater(S.Version.parse('10.0.0'), S.Version.parse('2.0.0'))
add("test_ordering__029", _t29)

def _t30(self):
    self.assertEqual(S.Version.parse('1.2.3+a') == S.Version.parse('1.2.3+b'), True)
add("test_equality__030", _t30)

def _t31(self):
    self.assertEqual(S.Version.parse('1.2.3-alpha') == S.Version.parse('1.2.3'), False)
add("test_equality__031", _t31)

def _t32(self):
    self.assertEqual(str(S.Version.parse('1.2.3').bump('major')), '2.0.0')
add("test_bump__032", _t32)

def _t33(self):
    self.assertEqual(str(S.Version.parse('1.2.3').bump('minor')), '1.3.0')
add("test_bump__033", _t33)

def _t34(self):
    self.assertEqual(str(S.Version.parse('1.2.3').bump('patch')), '1.2.4')
add("test_bump__034", _t34)

def _t35(self):
    self.assertEqual(str(S.Version.parse('1.2.3-alpha').bump('patch')), '1.2.3')
add("test_bump__035", _t35)

def _t36(self):
    self.assertEqual(str(S.Version.parse('1.2.0-alpha').bump('minor')), '1.2.0')
add("test_bump__036", _t36)

def _t37(self):
    self.assertEqual(str(S.Version.parse('1.0.0-alpha').bump('major')), '1.0.0')
add("test_bump__037", _t37)

def _t38(self):
    self.assertEqual(str(S.Version.parse('1.2.3').bump('prerelease')), '1.2.4-0')
add("test_bump__038", _t38)

def _t39(self):
    self.assertEqual(str(S.Version.parse('1.2.3-alpha').bump('prerelease')), '1.2.3-alpha.0')
add("test_bump__039", _t39)

def _t40(self):
    self.assertEqual(str(S.Version.parse('1.2.3-alpha.1').bump('prerelease')), '1.2.3-alpha.2')
add("test_bump__040", _t40)

def _t41(self):
    self.assertEqual(str(S.Version.parse('1.2.3-alpha.beta').bump('prerelease')), '1.2.3-alpha.beta.0')
add("test_bump__041", _t41)

def _t42(self):
    self.assertEqual(str(S.Version.parse('1.2.3-0').bump('prerelease')), '1.2.3-1')
add("test_bump__042", _t42)

def _t43(self):
    self.assertEqual(str(S.Version.parse('1.2.3+build').bump('patch')), '1.2.4')
add("test_bump__043", _t43)

def _t44(self):
    self.assertIs(bool(S.satisfies('1.2.3', '1.2.3')), True, msg="1.2.3 in '1.2.3'")
add("test_range_basic__044", _t44)

def _t45(self):
    self.assertIs(bool(S.satisfies('1.2.3', '=1.2.3')), True, msg="1.2.3 in '=1.2.3'")
add("test_range_basic__045", _t45)

def _t46(self):
    self.assertIs(bool(S.satisfies('1.2.4', '1.2.3')), False, msg="1.2.4 in '1.2.3'")
add("test_range_basic__046", _t46)

def _t47(self):
    self.assertIs(bool(S.satisfies('1.2.3', '>1.2.2')), True, msg="1.2.3 in '>1.2.2'")
add("test_range_basic__047", _t47)

def _t48(self):
    self.assertIs(bool(S.satisfies('1.2.3', '>1.2.3')), False, msg="1.2.3 in '>1.2.3'")
add("test_range_basic__048", _t48)

def _t49(self):
    self.assertIs(bool(S.satisfies('1.2.3', '>=1.2.3')), True, msg="1.2.3 in '>=1.2.3'")
add("test_range_basic__049", _t49)

def _t50(self):
    self.assertIs(bool(S.satisfies('1.2.3', '<1.2.3')), False, msg="1.2.3 in '<1.2.3'")
add("test_range_basic__050", _t50)

def _t51(self):
    self.assertIs(bool(S.satisfies('1.2.3', '<=1.2.3')), True, msg="1.2.3 in '<=1.2.3'")
add("test_range_basic__051", _t51)

def _t52(self):
    self.assertIs(bool(S.satisfies('1.5.0', '>=1.2.0 <2.0.0')), True, msg="1.5.0 in '>=1.2.0 <2.0.0'")
add("test_range_basic__052", _t52)

def _t53(self):
    self.assertIs(bool(S.satisfies('2.0.0', '>=1.2.0 <2.0.0')), False, msg="2.0.0 in '>=1.2.0 <2.0.0'")
add("test_range_basic__053", _t53)

def _t54(self):
    self.assertIs(bool(S.satisfies('3.0.0', '1.x || >=2.5.0')), True, msg="3.0.0 in '1.x || >=2.5.0'")
add("test_range_xrange__054", _t54)

def _t55(self):
    self.assertIs(bool(S.satisfies('2.4.9', '1.x || >=2.5.0')), False, msg="2.4.9 in '1.x || >=2.5.0'")
add("test_range_xrange__055", _t55)

def _t56(self):
    self.assertIs(bool(S.satisfies('1.9.9', '1.x || >=2.5.0')), True, msg="1.9.9 in '1.x || >=2.5.0'")
add("test_range_xrange__056", _t56)

def _t57(self):
    self.assertIs(bool(S.satisfies('1.9.9', '^1.2.3')), True, msg="1.9.9 in '^1.2.3'")
add("test_range_caret__057", _t57)

def _t58(self):
    self.assertIs(bool(S.satisfies('2.0.0', '^1.2.3')), False, msg="2.0.0 in '^1.2.3'")
add("test_range_caret__058", _t58)

def _t59(self):
    self.assertIs(bool(S.satisfies('1.2.2', '^1.2.3')), False, msg="1.2.2 in '^1.2.3'")
add("test_range_caret__059", _t59)

def _t60(self):
    self.assertIs(bool(S.satisfies('0.2.9', '^0.2.3')), True, msg="0.2.9 in '^0.2.3'")
add("test_range_caret__060", _t60)

def _t61(self):
    self.assertIs(bool(S.satisfies('0.3.0', '^0.2.3')), False, msg="0.3.0 in '^0.2.3'")
add("test_range_caret__061", _t61)

def _t62(self):
    self.assertIs(bool(S.satisfies('0.0.3', '^0.0.3')), True, msg="0.0.3 in '^0.0.3'")
add("test_range_caret__062", _t62)

def _t63(self):
    self.assertIs(bool(S.satisfies('0.0.4', '^0.0.3')), False, msg="0.0.4 in '^0.0.3'")
add("test_range_caret__063", _t63)

def _t64(self):
    self.assertIs(bool(S.satisfies('1.9.0', '^1.2.x')), True, msg="1.9.0 in '^1.2.x'")
add("test_range_caret__064", _t64)

def _t65(self):
    self.assertIs(bool(S.satisfies('0.0.9', '^0.0.x')), True, msg="0.0.9 in '^0.0.x'")
add("test_range_caret__065", _t65)

def _t66(self):
    self.assertIs(bool(S.satisfies('0.1.0', '^0.0.x')), False, msg="0.1.0 in '^0.0.x'")
add("test_range_caret__066", _t66)

def _t67(self):
    self.assertIs(bool(S.satisfies('0.0.0', '^0.0')), True, msg="0.0.0 in '^0.0'")
add("test_range_caret__067", _t67)

def _t68(self):
    self.assertIs(bool(S.satisfies('0.1.0', '^0.0')), False, msg="0.1.0 in '^0.0'")
add("test_range_caret__068", _t68)

def _t69(self):
    self.assertIs(bool(S.satisfies('1.2.9', '~1.2.3')), True, msg="1.2.9 in '~1.2.3'")
add("test_range_tilde__069", _t69)

def _t70(self):
    self.assertIs(bool(S.satisfies('1.3.0', '~1.2.3')), False, msg="1.3.0 in '~1.2.3'")
add("test_range_tilde__070", _t70)

def _t71(self):
    self.assertIs(bool(S.satisfies('1.2.0', '~1.2')), True, msg="1.2.0 in '~1.2'")
add("test_range_tilde__071", _t71)

def _t72(self):
    self.assertIs(bool(S.satisfies('1.3.0', '~1.2')), False, msg="1.3.0 in '~1.2'")
add("test_range_tilde__072", _t72)

def _t73(self):
    self.assertIs(bool(S.satisfies('1.9.0', '~1')), True, msg="1.9.0 in '~1'")
add("test_range_tilde__073", _t73)

def _t74(self):
    self.assertIs(bool(S.satisfies('2.0.0', '~1')), False, msg="2.0.0 in '~1'")
add("test_range_tilde__074", _t74)

def _t75(self):
    self.assertIs(bool(S.satisfies('0.2.5', '~0.2.3')), True, msg="0.2.5 in '~0.2.3'")
add("test_range_tilde__075", _t75)

def _t76(self):
    self.assertIs(bool(S.satisfies('0.3.0', '~0.2.3')), False, msg="0.3.0 in '~0.2.3'")
add("test_range_tilde__076", _t76)

def _t77(self):
    self.assertIs(bool(S.satisfies('1.9.9', '1.x')), True, msg="1.9.9 in '1.x'")
add("test_range_xrange__077", _t77)

def _t78(self):
    self.assertIs(bool(S.satisfies('2.0.0', '1.x')), False, msg="2.0.0 in '1.x'")
add("test_range_xrange__078", _t78)

def _t79(self):
    self.assertIs(bool(S.satisfies('1.2.7', '1.2.*')), True, msg="1.2.7 in '1.2.*'")
add("test_range_xrange__079", _t79)

def _t80(self):
    self.assertIs(bool(S.satisfies('1.3.0', '1.2.*')), False, msg="1.3.0 in '1.2.*'")
add("test_range_xrange__080", _t80)

def _t81(self):
    self.assertIs(bool(S.satisfies('5.6.7', '*')), True, msg="5.6.7 in '*'")
add("test_range_xrange__081", _t81)

def _t82(self):
    self.assertIs(bool(S.satisfies('5.6.7', '')), True, msg="5.6.7 in ''")
add("test_range_xrange__082", _t82)

def _t83(self):
    self.assertIs(bool(S.satisfies('0.0.0', '*')), True, msg="0.0.0 in '*'")
add("test_range_xrange__083", _t83)

def _t84(self):
    self.assertIs(bool(S.satisfies('1.2.3', '1.2.3 - 2.3.4')), True, msg="1.2.3 in '1.2.3 - 2.3.4'")
add("test_range_hyphen__084", _t84)

def _t85(self):
    self.assertIs(bool(S.satisfies('2.3.4', '1.2.3 - 2.3.4')), True, msg="2.3.4 in '1.2.3 - 2.3.4'")
add("test_range_hyphen__085", _t85)

def _t86(self):
    self.assertIs(bool(S.satisfies('2.3.5', '1.2.3 - 2.3.4')), False, msg="2.3.5 in '1.2.3 - 2.3.4'")
add("test_range_hyphen__086", _t86)

def _t87(self):
    self.assertIs(bool(S.satisfies('1.2.0', '1.2 - 2.3.4')), True, msg="1.2.0 in '1.2 - 2.3.4'")
add("test_range_hyphen__087", _t87)

def _t88(self):
    self.assertIs(bool(S.satisfies('1.1.9', '1.2 - 2.3.4')), False, msg="1.1.9 in '1.2 - 2.3.4'")
add("test_range_hyphen__088", _t88)

def _t89(self):
    self.assertIs(bool(S.satisfies('2.3.9', '1.2.3 - 2.3')), True, msg="2.3.9 in '1.2.3 - 2.3'")
add("test_range_hyphen__089", _t89)

def _t90(self):
    self.assertIs(bool(S.satisfies('2.4.0', '1.2.3 - 2.3')), False, msg="2.4.0 in '1.2.3 - 2.3'")
add("test_range_hyphen__090", _t90)

def _t91(self):
    self.assertIs(bool(S.satisfies('2.9.9', '1.2.3 - 2')), True, msg="2.9.9 in '1.2.3 - 2'")
add("test_range_hyphen__091", _t91)

def _t92(self):
    self.assertIs(bool(S.satisfies('3.0.0', '1.2.3 - 2')), False, msg="3.0.0 in '1.2.3 - 2'")
add("test_range_hyphen__092", _t92)

def _t93(self):
    self.assertIs(bool(S.satisfies('1.2.3-alpha.7', '>1.2.3-alpha.3')), True, msg="1.2.3-alpha.7 in '>1.2.3-alpha.3'")
add("test_range_prerelease__093", _t93)

def _t94(self):
    self.assertIs(bool(S.satisfies('3.4.5-alpha.9', '>1.2.3-alpha.3')), False, msg="3.4.5-alpha.9 in '>1.2.3-alpha.3'")
add("test_range_prerelease__094", _t94)

def _t95(self):
    self.assertIs(bool(S.satisfies('3.4.5', '>1.2.3-alpha.3')), True, msg="3.4.5 in '>1.2.3-alpha.3'")
add("test_range_basic__095", _t95)

def _t96(self):
    self.assertIs(bool(S.satisfies('1.0.0-alpha', '*')), False, msg="1.0.0-alpha in '*'")
add("test_range_prerelease__096", _t96)

def _t97(self):
    self.assertIs(bool(S.satisfies('1.0.0-alpha', '>=0.9.0')), False, msg="1.0.0-alpha in '>=0.9.0'")
add("test_range_prerelease__097", _t97)

def _t98(self):
    self.assertIs(bool(S.satisfies('1.2.4-beta.2', '^1.2.4-beta.1')), True, msg="1.2.4-beta.2 in '^1.2.4-beta.1'")
add("test_range_prerelease__098", _t98)

def _t99(self):
    self.assertIs(bool(S.satisfies('1.2.5-beta.2', '^1.2.4-beta.1')), False, msg="1.2.5-beta.2 in '^1.2.4-beta.1'")
add("test_range_prerelease__099", _t99)

def _t100(self):
    self.assertIs(bool(S.satisfies('1.2.5', '^1.2.4-beta.1')), True, msg="1.2.5 in '^1.2.4-beta.1'")
add("test_range_caret__100", _t100)

def _t101(self):
    self.assertIs(bool(S.satisfies('1.2.3-rc.1', '1.2.3-rc.1')), True, msg="1.2.3-rc.1 in '1.2.3-rc.1'")
add("test_range_prerelease__101", _t101)

def _t102(self):
    self.assertIs(bool(S.satisfies('1.2.3+build.5', '1.2.3')), True, msg="1.2.3+build.5 in '1.2.3'")
add("test_range_basic__102", _t102)

def _t103(self):
    self.assertIs(bool(S.satisfies('v1.2.3', '^1.0.0')), True, msg="v1.2.3 in '^1.0.0'")
add("test_range_caret__103", _t103)

def _t104(self):
    self.assertIs(bool(S.satisfies('1.2.3', '>=1.0.0 <1.2.0 || >=1.2.3 <1.3.0')), True, msg="1.2.3 in '>=1.0.0 <1.2.0 || >=1.2.3 <1.3.0'")
add("test_range_basic__104", _t104)

def _t105(self):
    self.assertIs(bool(S.satisfies('1.2.1', '>=1.0.0 <1.2.0 || >=1.2.3 <1.3.0')), False, msg="1.2.1 in '>=1.0.0 <1.2.0 || >=1.2.3 <1.3.0'")
add("test_range_basic__105", _t105)

def _t106(self):
    self.assertIs(bool(S.satisfies('2.0.0-0', '<2.0.0')), False, msg="2.0.0-0 in '<2.0.0'")
add("test_range_prerelease__106", _t106)

def _t107(self):
    self.assertIs(bool(S.satisfies('1.9.9', '<2')), True, msg="1.9.9 in '<2'")
add("test_range_xrange__107", _t107)

def _t108(self):
    self.assertIs(bool(S.satisfies('2.0.0', '<=2')), True, msg="2.0.0 in '<=2'")
add("test_range_xrange__108", _t108)

def _t109(self):
    self.assertIs(bool(S.satisfies('2.9.0', '<=2')), True, msg="2.9.0 in '<=2'")
add("test_range_xrange__109", _t109)

def _t110(self):
    self.assertIs(bool(S.satisfies('3.0.0', '>2')), True, msg="3.0.0 in '>2'")
add("test_range_xrange__110", _t110)

def _t111(self):
    self.assertIs(bool(S.satisfies('2.9.9', '>2')), False, msg="2.9.9 in '>2'")
add("test_range_xrange__111", _t111)

def _t112(self):
    self.assertIs(bool(S.satisfies('2.1.0', '>=2.1')), True, msg="2.1.0 in '>=2.1'")
add("test_range_xrange__112", _t112)

def _t113(self):
    self.assertIs(bool(S.satisfies('2.0.9', '>=2.1')), False, msg="2.0.9 in '>=2.1'")
add("test_range_xrange__113", _t113)

def _t114(self):
    out = subprocess.run([sys.executable, os.path.join(HERE, "semver.py")] + ['sort', '1.10.0', '1.2.0', '1.0.0-rc.1', '1.0.0'], capture_output=True, text=True, timeout=30)
    self.assertEqual(out.returncode, 0)
    self.assertEqual(out.stdout.split(), ['1.0.0-rc.1', '1.0.0', '1.2.0', '1.10.0'])
add("test_cli__114", _t114)

def _t115(self):
    r = subprocess.run([sys.executable, os.path.join(HERE, "semver.py")] + ['satisfies', '1.2.9', '~1.2.3'], capture_output=True, text=True, timeout=30)
    self.assertEqual(r.returncode, 0)
add("test_cli__115", _t115)

def _t116(self):
    r = subprocess.run([sys.executable, os.path.join(HERE, "semver.py")] + ['satisfies', '1.3.0', '~1.2.3'], capture_output=True, text=True, timeout=30)
    self.assertEqual(r.returncode, 1)
add("test_cli__116", _t116)

if __name__ == "__main__":
    unittest.main()
