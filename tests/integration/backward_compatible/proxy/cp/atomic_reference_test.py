import pytest

from hazelcast.errors import DistributedObjectDestroyedError, ClassCastError
from hazelcast.serialization.api import IdentifiedDataSerializable

from tests.integration.backward_compatible.util import write_string_to_output
from tests.integration.backward_compatible.proxy.cp import CPTestCase
from tests.util import skip_if_server_version_older_than


class AppendString(IdentifiedDataSerializable):
    def __init__(self, suffix):
        self.suffix = suffix

    def write_data(self, object_data_output):
        write_string_to_output(object_data_output, self.suffix)

    def read_data(self, object_data_input):
        pass

    def get_factory_id(self):
        return 66

    def get_class_id(self):
        return 17


@pytest.mark.enterprise
class AtomicReferenceTest(CPTestCase):
    def setUp(self):
        self.ref = self.client.cp_subsystem.get_atomic_reference("ref").blocking()

    def tearDown(self):
        self.ref.clear()

    def test_ref_in_another_group(self):
        another_ref = self.client.cp_subsystem.get_atomic_reference("ref@mygroup").blocking()
        another_ref.set("hey")
        self.assertEqual("hey", another_ref.get())
        # the following value has to be None,
        # as `ref` belongs to the default CP group
        self.assertIsNone(self.ref.get())

    def test_use_after_destroy(self):
        another_ref = self.client.cp_subsystem.get_atomic_reference("another-ref").blocking()
        another_ref.destroy()
        # the next destroy call should be ignored
        another_ref.destroy()

        with self.assertRaises(DistributedObjectDestroyedError):
            another_ref.get()

        another_ref2 = self.client.cp_subsystem.get_atomic_reference("another-ref").blocking()
        with self.assertRaises(DistributedObjectDestroyedError):
            another_ref2.get()

    def test_initial_value(self):
        self.assertIsNone(self.ref.get())

    def test_compare_and_set_when_condition_is_met(self):
        self.assertTrue(self.ref.compare_and_set(None, 42))
        self.assertEqual(42, self.ref.get())
        self.assertTrue(self.ref.compare_and_set(42, "hey"))
        self.assertEqual("hey", self.ref.get())

    def test_compare_and_set_when_condition_is_not_met(self):
        self.assertFalse(self.ref.compare_and_set(42, 23))
        self.assertIsNone(self.ref.get())
        self.ref.set("a")
        self.assertFalse(self.ref.compare_and_set("b", "c"))
        self.assertEqual("a", self.ref.get())

    def test_get(self):
        self.ref.set([1, 2, 3])
        self.assertEqual([1, 2, 3], self.ref.get())

    def test_set(self):
        self.assertIsNone(self.ref.set("abc"))
        self.assertEqual("abc", self.ref.get())
        self.assertIsNone(self.ref.set(["another_type", 1]))
        self.assertEqual(["another_type", 1], self.ref.get())

    def test_get_and_set(self):
        self.assertIsNone(self.ref.get_and_set("42"))
        self.assertEqual("42", self.ref.get())
        self.assertEqual("42", self.ref.get_and_set(42))
        self.assertEqual(42, self.ref.get())

    def test_is_none(self):
        self.assertTrue(self.ref.is_none())
        self.ref.set(11)
        self.assertFalse(self.ref.is_none())
        self.ref.set(None)
        self.assertTrue(self.ref.is_none())

    def test_clear(self):
        self.assertIsNone(self.ref.clear())
        self.assertIsNone(self.ref.get())
        self.ref.set("str")
        self.assertEqual("str", self.ref.get())
        self.assertIsNone(self.ref.clear())
        self.assertIsNone(self.ref.get())

    def test_contains(self):
        self.assertTrue(self.ref.contains(None))
        self.ref.set("42")
        self.assertTrue(self.ref.contains("42"))
        self.assertFalse(self.ref.contains(42))
        self.assertFalse(self.ref.contains(None))
        self.ref.clear()
        self.assertFalse(self.ref.contains("42"))
        self.assertTrue(self.ref.contains(None))

    def test_alter(self):
        # the class is defined in the 4.1 JAR
        skip_if_server_version_older_than(self, self.client, "4.1")
        self.ref.set("hey")
        self.assertIsNone(self.ref.alter(AppendString("123")))
        self.assertEqual("hey123", self.ref.get())

    def test_alter_with_incompatible_types(self):
        # the class is defined in the 4.1 JAR
        skip_if_server_version_older_than(self, self.client, "4.1")
        self.ref.set(42)
        with self.assertRaises(ClassCastError):
            self.ref.alter(AppendString("."))

    def test_alter_and_get(self):
        # the class is defined in the 4.1 JAR
        skip_if_server_version_older_than(self, self.client, "4.1")
        self.ref.set("123")
        self.assertEqual("123...", self.ref.alter_and_get(AppendString("...")))
        self.assertEqual("123...", self.ref.get())

    def test_get_and_alter(self):
        # the class is defined in the 4.1 JAR
        skip_if_server_version_older_than(self, self.client, "4.1")
        self.ref.set("hell")
        self.assertEqual("hell", self.ref.get_and_alter(AppendString("o")))
        self.assertEqual("hello", self.ref.get())

    def test_apply(self):
        # the class is defined in the 4.1 JAR
        skip_if_server_version_older_than(self, self.client, "4.1")
        self.ref.set("hell")
        self.assertEqual("hello", self.ref.apply(AppendString("o")))
        self.assertEqual("hell", self.ref.get())
