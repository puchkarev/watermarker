import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import admin
import lambda_function
import payments
import watermarker
from test_payments import FakeTelegram, SECRET, _http
from test_quota import FakeDynamo, FREE_CHAT, UNLIMITED_CHAT, _ClientError

ADMIN = 900
BUYER = 111
PAYER = 777
COMMANDS = ["/admin", "/users", "/users 2", "/usage 111", "/charges 111", "/limits", "/stats",
            "/grant 111 50 goodwill", "/refund ch1", "/refund ch1 confirm",
            "/admins", "/promote 111 confirm", "/demote 900 confirm", "/unlimited", "/giveunlimited 111 confirm",
            "/takeunlimited 5173725149 confirm"]
PROMOTED = 555


def _missing(bucket, key, path):
    raise _ClientError("404")


class AdminTestCase(unittest.TestCase):

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "BOT_TOKEN": "bot-token", "WEBHOOK_SECRET": SECRET, "STATE_BUCKET": "bucket",
            "QUOTA_TABLE": "quota", "FREE_MONTHLY_IMAGES": "10", "SYSTEM_DAILY_IMAGES": "5000",
            "UNLIMITED_CHAT_IDS": str(UNLIMITED_CHAT), "ADMIN_CHAT_IDS": str(ADMIN), "ALLOWED_CHAT_IDS": "",
        })
        self.env.start()
        self.db = FakeDynamo(page_size=3)  # small pages, so every Query/Scan paginates
        lambda_function._clients.clear()
        lambda_function._clients.update({"dynamodb": self.db, "lambda": MagicMock()})
        self.telegram = FakeTelegram()
        self.requests = patch.object(payments.requests, "post", side_effect=self.telegram.post)
        self.requests.start()
        self.quota = lambda_function._quota()
        # One buyer: 10 free images used, a small pack bought, 5 of it spent
        self.quota.credit(BUYER, PAYER, "ch1", "small", 100, 200)
        self.quota.reserve(BUYER, 15)
        self.telegram.calls.clear()

    def tearDown(self):
        self.requests.stop()
        self.env.stop()
        lambda_function._clients.clear()

    def _run(self, text, chat=ADMIN):
        handled = admin.handle_command("bot-token", chat, text, self.quota, {str(ADMIN)})
        texts = self.telegram.texts()
        self.telegram.calls = [c for c in self.telegram.calls if c[0] != "sendMessage"]
        return handled, (texts[-1] if texts else None)

    def _reply(self, text):
        handled, reply = self._run(text)
        self.assertTrue(handled)
        return reply


class TestReadCommands(AdminTestCase):

    def test_admin_lists_the_commands(self):
        self.assertIn("/refund <charge id> confirm", self._reply("/admin"))

    def test_usage_of_one_chat(self):
        reply = self._reply("/usage 111")
        self.assertIn("This month: 15 images, 10 of 10 free", reply)
        self.assertIn("Bought images left: 95", reply)
        self.assertIn("Lifetime: 15 images", reply)
        self.assertIn("small: 100 images for 200 Stars, payer 777, charge ch1", reply)

    def test_usage_needs_a_chat_id(self):
        self.assertEqual(self._reply("/usage"), "Usage: /usage <chat id>")
        self.assertEqual(self._reply("/usage abc"), "Usage: /usage <chat id>")

    def test_users_most_active_first_and_paginated(self):
        self.quota.reserve(222, 3)
        self.quota.reserve(UNLIMITED_CHAT, 40)
        with patch.object(admin, "PAGE_SIZE", 2):
            first, second = self._reply("/users"), self._reply("/users 2")
        self.assertEqual(first.splitlines(), [
            "Chats 3 - page 1 of 2 (this month / free / bought left / lifetime)",
            f"{UNLIMITED_CHAT} (unlimited): 40 / 0 / 0 / 40",
            "111: 15 / 10 / 95 / 15",
            "Next: /users 2"])
        self.assertEqual(second.splitlines()[1:], ["222: 3 / 3 / 0 / 3"])

    def test_users_pages_cover_tied_chats_exactly_once_in_chat_id_order(self):
        # Same usage, no last_seen: only the chat id can order them, whatever the Scan order
        for chat in (505, 303, 404):
            self.db.items[(f"chat#{chat}", "profile")] = {"messages": 1}
        with patch.object(admin, "PAGE_SIZE", 2):
            pages = [self._reply("/users"), self._reply("/users 2")]
        listed = [line.split(":")[0] for page in pages for line in page.splitlines()[1:]
                  if not line.startswith("Next")]
        self.assertEqual(listed, ["111", "303", "404", "505"])

    def test_users_includes_a_chat_with_only_free_usage(self):
        # Its history row missing, e.g. because that best-effort write failed
        self.db.items[("chat#333", "usage#2026-09")] = {"images": 4, "expires_at": 0}
        with patch.object(self.quota, "now", return_value=self.quota.now().replace(year=2026, month=9)):
            self.assertIn("333: 0 / 4 / 0 / 0", self._reply("/users"))

    def test_charges(self):
        self.quota.credit(BUYER, PAYER, "ch2", "large", 1000, 1000)
        reply = self._reply("/charges 111").splitlines()
        self.assertEqual(len(reply), 3)
        self.assertIn("charge ch1", reply[1] + reply[2])
        self.assertEqual(self._reply("/charges 5"), "Chat 5 hasn't bought anything.")

    def test_limits(self):
        reply = self._reply("/limits")
        self.assertIn("Free images per chat: 10 a month", reply)
        self.assertIn("5000 a day - 10 used today, 4990 left", reply)
        self.assertIn(f"Unlimited chats: {UNLIMITED_CHAT}", reply)

    def test_stats(self):
        self.quota.reserve(222, 2)
        reply = self._reply("/stats")
        self.assertIn("This month: 17 images for 2 chats", reply)
        self.assertIn("Sold: 100 images for 200 Stars (0 refunded purchases not counted)", reply)
        self.assertIn("Bought images not yet used: 95", reply)

    def test_admin_commands_use_no_allowance(self):
        before = {k: dict(v) for k, v in self.db.items.items()}
        for command in ["/admin", "/users", "/usage 111", "/charges 111", "/limits", "/stats", "/refund ch1"]:
            self._reply(command)
        self.assertEqual(self.db.items, before)


class TestProfiles(AdminTestCase):
    """#45: a chat that only ever talks to the bot still shows up, with a name."""

    def _send(self, chat, text="/help"):
        update = {"update_id": 9, "message": {"chat": chat, "from": {"id": chat["id"]}, "text": text}}
        lambda_function.handler(_http(update), MagicMock(invoked_function_arn="arn"))

    def test_command_only_chat_appears_in_users_with_its_name(self):
        self._send({"id": 444, "type": "private", "username": "tester", "first_name": "Tess"})
        self._send({"id": 444, "type": "private", "username": "tester", "first_name": "Tess"}, "/balance")
        self._send({"id": -100777, "type": "group", "title": "Photo club"})
        users = self._reply("/users")
        line = next(l for l in users.splitlines() if l.startswith("444"))
        self.assertTrue(line.startswith("444 (@tester): 0 / 0 / 0 / 0, last seen "), line)
        self.assertIn("-100777 (Photo club): 0 / 0 / 0 / 0, last seen", users)
        usage = self._reply("/usage 444")
        self.assertIn("Chat 444 (@tester)", usage)
        self.assertIn(", 2 messages", usage)

    def test_name_falls_back_to_first_and_last_name(self):
        self._send({"id": 555, "type": "private", "first_name": "Ana", "last_name": "Diaz"})
        self.assertIn("555 (Ana Diaz)", self._reply("/users"))

    def test_active_chats_are_counted_in_stats(self):
        self._send({"id": 444, "type": "private", "username": "tester"})
        self._send({"id": 555, "type": "private", "first_name": "Ana"})
        self.assertIn("; 2 chats used the bot", self._reply("/stats"))

    def test_every_admin_command_is_logged(self):
        with patch("builtins.print") as mock_print:
            self._reply("/usage 111")
        self.assertTrue(any("ADMIN COMMAND admin=900 command=/usage args='111'" in str(c)
                            for c in mock_print.call_args_list))


class TestGrant(AdminTestCase):

    def test_grant_adds_credits_and_is_audited(self):
        with patch("builtins.print") as mock_print:
            reply = self._reply("/grant 111 50 billing bug on 26 Sep")
        self.assertEqual(reply, "Granted 50 images to chat 111. Its balance went from 95 to 145.")
        self.assertEqual(self.quota.balance(BUYER), 145)
        audit = [str(c) for c in mock_print.call_args_list if "ADMIN AUDIT" in str(c)]
        self.assertEqual(len(audit), 1)
        for part in ("admin=900", "action=grant", "chat=111", "images=50", "reason='billing bug on 26 Sep'",
                     "balance_before=95", "balance_after=145"):
            self.assertIn(part, audit[0])

    def test_grant_is_recorded_with_who_and_why(self):
        self._reply("/grant 111 50 billing bug")
        (sk, row), = [(sk, a) for sk, a in self.quota.chat_records(BUYER).items() if sk.startswith("grant#")]
        self.assertEqual((row["images"], row["admin"], row["reason"]), (50, str(ADMIN), "billing bug"))
        self.assertNotIn("expires_at", row)

    def test_grant_rejects_bad_amounts(self):
        for text in ("/grant 111", "/grant 111 0", "/grant 111 -5", "/grant 111 lots", "/grant 111 1000000"):
            self.assertIn("Usage: /grant", self._reply(text))
        self.assertEqual(self.quota.balance(BUYER), 95)


class TestRefund(AdminTestCase):

    def _refund_calls(self):
        return [body for method, body in self.telegram.calls if method == "refundStarPayment"]

    def _audit(self, mock_print):
        return [str(c) for c in mock_print.call_args_list if "ADMIN AUDIT" in str(c)]

    def test_refund_shows_what_would_happen_before_doing_anything(self):
        reply = self._reply("/refund ch1")
        self.assertIn("Refunding sends 200 Stars back to user 777 and takes 100 images from chat 111 "
                      "(balance 95 -> 0)", reply)
        self.assertIn("so 5 have been used. Counting the oldest first, 5 of this pack's 100 images were "
                      "among them.", reply)
        self.assertIn("Only 95 images are left, so the balance will go to 0.", reply)
        self.assertIn("/refund ch1 confirm", reply)
        self.assertEqual(self._refund_calls(), [])
        self.assertEqual(self.quota.balance(BUYER), 95)

    def test_refunding_a_spent_pack_says_whose_images_it_takes(self):
        # ch1 fully spent, then an untouched later pack: the refund would take ch2's images
        self.quota.reserve(BUYER, 95)
        self.quota.credit(BUYER, PAYER, "ch2", "large", 1000, 1000)
        reply = self._reply("/refund ch1")
        self.assertIn("(balance 1000 -> 900)", reply)
        self.assertIn("Chat 111 has been given 1100 bought or granted images and has 1000 left, so 100 have "
                      "been used. Counting the oldest first, 100 of this pack's 100 images were among them. "
                      "The refund therefore takes 100 images that came from other purchases or grants.", reply)
        # The later, untouched pack is described as untouched
        self.assertNotIn("have been used", self._reply("/refund ch2"))

    def test_confirmed_refund_pays_the_payer_takes_the_credits_and_is_audited(self):
        with patch("builtins.print") as mock_print:
            reply = self._reply("/refund ch1 confirm")
        # The payer's user id, recorded at purchase: in a group it isn't the chat id
        self.assertEqual(self._refund_calls(), [{"user_id": PAYER, "telegram_payment_charge_id": "ch1"}])
        self.assertEqual(self.quota.balance(BUYER), 0)
        self.assertEqual(reply, "Refunded 200 Stars to user 777 for charge ch1. Took 95 images from chat 111; "
                                "its balance is now 0. 5 could not be taken back: the chat had no images left.")
        audit = self._audit(mock_print)
        self.assertEqual(len(audit), 1)
        for part in ("action=refund", "charge='ch1'", "user='777'", "stars=200", "images_taken=95",
                     "balance_before=95", "balance_after=0", "pack_images_used=5"):
            self.assertIn(part, audit[0])
        charge = self.quota.charge("ch1")
        self.assertTrue(charge["refunded"])
        self.assertNotIn("refund_pending", charge)
        self.assertIn("REFUNDED", self._reply("/charges 111"))

    def test_refund_is_idempotent(self):
        self._reply("/refund ch1 confirm")
        self.assertIn("already refunded", self._reply("/refund ch1 confirm"))
        self.assertIn("already refunded", self._reply("/refund ch1"))
        self.assertEqual(len(self._refund_calls()), 1)

    def test_only_one_finish_can_take_the_images_back(self):
        # Two confirms racing past Telegram (the second is told CHARGE_ALREADY_REFUNDED)
        self.assertTrue(self.quota.begin_refund("ch1", BUYER))
        self.assertTrue(self.quota.begin_refund("ch1", BUYER))  # a pending refund can be resumed
        self.assertTrue(self.quota.finish_refund("ch1", BUYER))
        self.assertFalse(self.quota.finish_refund("ch1", BUYER))
        self.assertFalse(self.quota.begin_refund("ch1", BUYER))

    def test_unspent_pack_is_taken_back_whole(self):
        self.quota.credit(BUYER, PAYER, "ch2", "large", 1000, 1000)
        reply = self._reply("/refund ch2 confirm")
        self.assertIn("Took 1000 images from chat 111; its balance is now 95.", reply)
        self.assertNotIn("could not be taken back", reply)

    def test_definite_refusal_changes_nothing(self):
        self.telegram.refuse["refundStarPayment"] = "USER_BOT_REQUIRED"
        with patch("builtins.print") as mock_print:
            reply = self._reply("/refund ch1 confirm")
        self.assertEqual(reply, "Telegram refused the refund, so nothing changed: "
                                "refundStarPayment failed: USER_BOT_REQUIRED")
        self.assertTrue(any("action=refund-failed" in line for line in self._audit(mock_print)))
        self.assertEqual(self.quota.balance(BUYER), 95)
        charge = self.quota.charge("ch1")
        self.assertNotIn("refunded", charge)
        self.assertNotIn("refund_pending", charge)
        # Nothing stayed flagged, so it can be tried again
        del self.telegram.refuse["refundStarPayment"]
        self.assertIn("Refunded 200 Stars", self._reply("/refund ch1 confirm"))

    def test_already_refunded_at_telegram_counts_as_done(self):
        self.telegram.refuse["refundStarPayment"] = "CHARGE_ALREADY_REFUNDED"
        reply = self._reply("/refund ch1 confirm")
        self.assertIn("Refunded 200 Stars to user 777", reply)
        self.assertEqual(self.quota.balance(BUYER), 0)
        self.assertTrue(self.quota.charge("ch1")["refunded"])

    def test_no_answer_leaves_it_pending_and_a_retry_finishes_it_once(self):
        self.telegram.unreachable.add("refundStarPayment")
        with patch("builtins.print") as mock_print:
            reply = self._reply("/refund ch1 confirm")
        self.assertIn("may or may not have gone back", reply)
        self.assertIn("Send /refund ch1 confirm again to finish", reply)
        self.assertTrue(any("action=refund-unknown" in line for line in self._audit(mock_print)))
        charge = self.quota.charge("ch1")
        self.assertTrue(charge["refund_pending"])
        self.assertNotIn("refunded", charge)
        self.assertEqual(self.quota.balance(BUYER), 95)  # images untouched until it's known
        self.assertIn("REFUND PENDING", self._reply("/charges 111"))
        self.assertIn("its outcome is unknown", self._reply("/refund ch1"))

        # The first call did reach Telegram: the retry is told so, and finishes the job once
        self.telegram.unreachable.clear()
        self.telegram.refuse["refundStarPayment"] = "CHARGE_ALREADY_REFUNDED"
        self.assertIn("Refunded 200 Stars", self._reply("/refund ch1 confirm"))
        self.assertEqual(self.quota.balance(BUYER), 0)
        self.assertIn("already refunded", self._reply("/refund ch1 confirm"))
        self.assertEqual(self.quota.balance(BUYER), 0)

    def test_server_error_is_not_mistaken_for_a_refusal(self):
        self.telegram.refuse["refundStarPayment"] = ("Internal Server Error", 500)
        self.assertIn("may or may not have gone back", self._reply("/refund ch1 confirm"))
        self.assertTrue(self.quota.charge("ch1")["refund_pending"])

    def test_failed_rollback_is_loud_and_leaves_a_resolvable_pending_refund(self):
        self.telegram.refuse["refundStarPayment"] = "USER_BOT_REQUIRED"
        original = self.db.update_item

        def rollback_fails(**kwargs):
            if kwargs["UpdateExpression"] == "REMOVE refund_pending":
                raise _ClientError("InternalServerError")
            return original(**kwargs)
        self.db.update_item = rollback_fails
        with patch("builtins.print") as mock_print:
            reply = self._reply("/refund ch1 confirm")
        logged = [str(c) for c in mock_print.call_args_list]
        self.assertIn("Telegram refused the refund, so nothing changed", reply)
        self.assertTrue(any("REFUND ROLLBACK FAILED charge=ch1" in line for line in logged))
        self.assertTrue(any("action=refund-failed" in line for line in logged))
        # Stuck pending rather than stuck refunded: the next attempt resolves it
        self.db.update_item = original
        self.assertIn("Telegram refused the refund", self._reply("/refund ch1 confirm"))
        self.assertNotIn("refund_pending", self.quota.charge("ch1"))

    def test_refund_whose_credit_deduction_fails_is_still_audited(self):
        with patch.object(self.quota, "deduct", side_effect=_ClientError("InternalServerError")), \
                patch("builtins.print") as mock_print:
            reply = self._reply("/refund ch1 confirm")
        self.assertIn("Refunded 200 Stars to user 777", reply)
        self.assertIn("taking the 100 images back from chat 111 failed", reply)
        audit = self._audit(mock_print)
        self.assertEqual(len(audit), 1)
        self.assertIn("images_taken=0", audit[0])
        # Money moved, so it stays refunded: a retry must not refund it again
        self.assertIn("already refunded", self._reply("/refund ch1 confirm"))
        self.assertEqual(len(self._refund_calls()), 1)

    def test_unknown_charge(self):
        self.assertEqual(self._reply("/refund nope confirm"), "No purchase has charge id nope.")
        self.assertEqual(self._refund_calls(), [])


class TestReplies(AdminTestCase):

    def test_long_replies_are_split_under_telegrams_limit(self):
        for i in range(120):
            self.quota.credit(BUYER, PAYER, f"charge-with-a-long-id-{i:03d}", "small", 100, 200)
        with patch.object(admin, "PAGE_SIZE", 1000):
            self.assertTrue(admin.handle_command("bot-token", ADMIN, "/charges 111", self.quota, {str(ADMIN)}))
        texts = self.telegram.texts()
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(t) <= 4096 for t in texts))
        self.assertEqual(sum(t.count("charge-with-a-long-id-") for t in texts), 120)

    def test_charges_are_paginated(self):
        for i in range(3):
            self.quota.credit(BUYER, PAYER, f"c{i}", "small", 100, 200)
        with patch.object(admin, "PAGE_SIZE", 2):
            first, second = self._reply("/charges 111"), self._reply("/charges 111 2")
        self.assertIn("page 1 of 2", first)
        self.assertIn("Next: /charges 111 2", first)
        self.assertEqual(len(second.splitlines()), 3)  # header + the remaining 2 of 4

    def test_a_failed_reply_does_not_escape(self):
        self.telegram.refuse["sendMessage"] = "Bad Request: message is too long"
        with patch("builtins.print") as mock_print:
            self.assertTrue(admin.handle_command("bot-token", ADMIN, "/admin", self.quota, {str(ADMIN)}))
        self.assertTrue(any("admin reply to 900 failed" in str(c) for c in mock_print.call_args_list))


class TestPrivileges(AdminTestCase):

    def _audits(self, text):
        with patch("builtins.print") as log:
            reply = self._reply(text)
        return reply, [str(c) for c in log.call_args_list if "ADMIN AUDIT" in str(c)]

    def test_promote_previews_who_it_is_then_needs_confirm(self):
        self.quota.record_interaction({"id": PROMOTED, "type": "private", "username": "victor"})
        preview, audits = self._audits(f"/promote {PROMOTED}")
        self.assertIn(f"This makes {PROMOTED} (@victor) an admin: it can read every chat's usage", preview)
        self.assertIn(f"To go ahead: /promote {PROMOTED} confirm", preview)
        self.assertEqual(audits, [])
        self.assertFalse(lambda_function._quota().is_admin(PROMOTED))
        # An id the bot has never seen is flagged: likely a typo
        self.assertIn("556 (not seen by the bot recently - check the id)", self._reply("/promote 556"))

    def test_promote_is_audited_and_a_repeat_or_yourself_is_a_no_op(self):
        reply, audits = self._audits(f"/promote {PROMOTED} confirm")
        self.assertIn(f"Chat {PROMOTED} is now an admin", reply)
        self.assertEqual(len(audits), 1)
        self.assertIn(f"action=promote chat={PROMOTED} admin_before=False admin_after=True", audits[0])
        for again in (f"/promote {PROMOTED}", f"/promote {PROMOTED} confirm", f"/promote {ADMIN} confirm"):
            reply, audits = self._audits(again)
            self.assertIn("is already an admin. Nothing changed.", reply)
            self.assertEqual(audits, [])

    def test_promote_refuses_groups(self):
        reply, audits = self._audits("/promote -100123 confirm")
        self.assertIn("is a group", reply)
        self.assertEqual(audits, [])
        self.assertFalse(lambda_function._quota().is_admin(-100123))

    def test_granting_warns_when_the_deprecated_allowlist_would_refuse_the_chat(self):
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": f"{ADMIN}"}):
            for command in (f"/promote {PROMOTED}", f"/promote {PROMOTED} confirm",
                            "/giveunlimited 222", "/giveunlimited 222 confirm"):
                self.assertIn("ALLOWED_CHAT_IDS is set and doesn't include it", self._reply(command), command)
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": f"{ADMIN},223"}):
            self.assertNotIn("ALLOWED_CHAT_IDS", self._reply("/giveunlimited 223"))

    def test_root_admins_cannot_be_demoted_even_by_themselves(self):
        reply, audits = self._audits(f"/demote {ADMIN} confirm")
        self.assertEqual(reply, f"{ADMIN} is a root admin from the deployment config and can't be demoted here.")
        self.assertEqual(audits, [])
        self.assertTrue(lambda_function._quota().is_admin(ADMIN))

    def test_demoting_a_root_admin_removes_a_grant_underneath_so_leaving_the_config_revokes(self):
        self._reply(f"/promote {PROMOTED} confirm")
        with patch.dict(os.environ, {"ADMIN_CHAT_IDS": f"{ADMIN},{PROMOTED}"}):
            self.quota = lambda_function._quota()
            self.assertIn(f"- {PROMOTED}: root, from the deployment config, and added by {ADMIN}",
                          self._reply("/admins"))
            reply, audits = self._audits(f"/demote {PROMOTED}")
            self.assertIn("can't be demoted here. It had also been made an admin from chat; that was removed", reply)
            self.assertIn(f"action=demote chat={PROMOTED} root=True row_removed=True", audits[0])
            self.assertTrue(lambda_function._quota().is_admin(PROMOTED))
        self.assertFalse(lambda_function._quota().is_admin(PROMOTED))

    def test_demote_needs_confirm_and_warns_about_the_last_promoted_admin_and_yourself(self):
        self._reply(f"/promote {PROMOTED} confirm")
        preview = self._reply(f"/demote {PROMOTED}")
        self.assertIn(f"To go ahead: /demote {PROMOTED} confirm", preview)
        self.assertIn("last admin added from chat", preview)
        self.assertTrue(lambda_function._quota().is_admin(PROMOTED))
        # From the promoted admin's own chat, about itself
        _, preview = self._run(f"/demote {PROMOTED}", chat=PROMOTED)
        self.assertIn("you will lose admin access yourself", preview)
        reply, audits = self._audits(f"/demote {PROMOTED} confirm")
        self.assertEqual(reply, f"Chat {PROMOTED} is no longer an admin.")
        self.assertIn(f"action=demote chat={PROMOTED} admin_before=True admin_after=False", audits[0])
        self.assertFalse(lambda_function._quota().is_admin(PROMOTED))
        self.assertIn("isn't an admin", self._reply(f"/demote {PROMOTED} confirm"))

    def test_lists_mark_the_root_entries(self):
        self._reply(f"/promote {PROMOTED} confirm")
        self._reply("/giveunlimited 222 confirm")
        admins = self._reply("/admins").splitlines()
        self.assertEqual(admins[0], "Admins (2):")
        self.assertTrue(admins[1].startswith(f"- {PROMOTED}: added by {ADMIN} on 20"), admins)
        self.assertIn(f"- {ADMIN}: root, from the deployment config", admins)
        unlimited = self._reply("/unlimited").splitlines()
        self.assertIn(f"- {UNLIMITED_CHAT}: root, from the deployment config", unlimited)
        self.assertTrue(any(line.startswith(f"- 222: added by {ADMIN} on 20") for line in unlimited), unlimited)

    def test_unlimited_can_be_given_and_taken_and_the_chat_pays_again_at_once(self):
        # 222 has bought a pack and used none of its free images
        self.quota.credit(222, 222, "ch2", "small", 100, 200)
        self.assertIn("To go ahead: /giveunlimited 222 confirm", self._reply("/giveunlimited 222"))
        self.assertFalse(lambda_function._quota().is_unlimited(222))
        reply, audits = self._audits("/giveunlimited 222 confirm")
        self.assertIn("is now unlimited", reply)
        self.assertIn("action=giveunlimited chat=222 unlimited_before=False unlimited_after=True", audits[0])
        self.assertIn("already unlimited", self._reply("/giveunlimited 222 confirm"))

        job = lambda_function._quota()
        self.assertIsNone(job.reserve(222, 30))
        self.assertEqual((job.status(222)["free_left"], job.balance(222)), (10, 100))

        preview = self._reply("/takeunlimited 222")
        self.assertIn("10 of 10 left this month), then its bought images (100)", preview)
        self.assertTrue(lambda_function._quota().is_unlimited(222))
        reply, audits = self._audits("/takeunlimited 222 confirm")
        self.assertIn("is no longer unlimited", reply)
        self.assertIn("action=takeunlimited chat=222 unlimited_before=True unlimited_after=False", audits[0])

        # The very next job: free images first, then credits
        job = lambda_function._quota()
        self.assertIsNone(job.reserve(222, 15))
        self.assertEqual((job.status(222)["free_left"], job.balance(222)), (0, 95))

    def test_root_unlimited_chats_cannot_be_limited(self):
        reply, audits = self._audits(f"/takeunlimited {UNLIMITED_CHAT} confirm")
        self.assertEqual(reply, f"{UNLIMITED_CHAT} is unlimited from the deployment config and can't be changed here.")
        self.assertEqual(audits, [])
        self.assertIn("isn't unlimited", self._reply(f"/takeunlimited {BUYER}"))

    def test_privilege_commands_need_a_chat_id(self):
        for command in ("/promote", "/demote x", "/giveunlimited", "/takeunlimited"):
            self.assertTrue(self._reply(command).startswith("Usage: "), command)


class TestGating(AdminTestCase):
    """A non-admin must not be able to tell the admin commands exist."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.work = patch.object(lambda_function, "WORK_DIR", self.tmp)
        self.work.start()
        s3 = MagicMock()
        s3.download_file.side_effect = _missing
        lambda_function._clients["s3"] = s3
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        self.work.stop()
        shutil.rmtree(self.tmp)
        watermarker.QUOTA = None
        watermarker.EXTRA_COMMANDS = None
        watermarker.EXTRA_HELP = ""
        super().tearDown()

    def _observe(self, chat, text):
        """Everything the bot does in reply to text: messages sent and lines logged."""
        self.telegram.calls.clear()
        update = {"update_id": 5, "message": {"chat": {"id": chat}, "text": text}}
        with patch.object(watermarker.tele, "send_telegram") as bot_send, patch("builtins.print") as log:
            lambda_function.handler({lambda_function.TASK_KEY: update, "secret": SECRET}, MagicMock())
        return list(self.telegram.calls), bot_send.call_args_list, [str(c) for c in log.call_args_list]

    def test_non_admins_get_exactly_the_unknown_command_behaviour(self):
        unknown = self._observe(BUYER, "/asdf")
        self.assertEqual(unknown, ([], [], []))
        for command in COMMANDS:
            for chat in (BUYER, UNLIMITED_CHAT):  # unlimited is not admin
                self.assertEqual(self._observe(chat, command), unknown, (chat, command))
        self.assertEqual(self.quota.balance(BUYER), 95)
        self.assertIsNone(self.quota.charge("ch1").get("refunded"))
        fresh = lambda_function._quota()
        self.assertFalse(fresh.is_admin(BUYER) or fresh.is_unlimited(BUYER))
        self.assertTrue(fresh.is_admin(ADMIN) and fresh.is_unlimited(UNLIMITED_CHAT))

    def test_a_promoted_admin_can_use_admin_commands_until_demoted(self):
        self.assertEqual(self._observe(PROMOTED, "/stats")[0], [])
        self._observe(ADMIN, f"/promote {PROMOTED} confirm")
        sent, _, _ = self._observe(PROMOTED, "/stats")
        self.assertIn("This month:", sent[0][1]["text"])
        # A promoted admin can promote and demote others, but not the root
        self._observe(PROMOTED, "/promote 556 confirm")
        sent, _, _ = self._observe(PROMOTED, f"/demote {ADMIN} confirm")
        self.assertIn("root admin", sent[0][1]["text"])
        self._observe(ADMIN, f"/demote {PROMOTED}")  # a preview changes nothing
        self.assertNotEqual(self._observe(PROMOTED, "/stats")[0], [])
        self._observe(ADMIN, f"/demote {PROMOTED} confirm")
        self.assertEqual(self._observe(PROMOTED, "/stats"), ([], [], []))
        self.assertEqual(self._observe(ADMIN, "/admin")[0][0][0], "sendMessage")

    def test_privilege_commands_stay_out_of_the_public_menu(self):
        public = set(watermarker.BOT_COMMANDS) | set(payments.PAYMENT_COMMANDS)
        for command in admin.COMMANDS:
            self.assertNotIn(command.lstrip("/"), public)

    def test_admin_chat_gets_answers_through_the_worker(self):
        sent, _, _ = self._observe(ADMIN, "/admin")
        self.assertEqual(sent[0][0], "sendMessage")
        self.assertIn("Admin commands", sent[0][1]["text"])

    def test_no_admins_configured_means_no_admin_commands(self):
        with patch.dict(os.environ, {"ADMIN_CHAT_IDS": ""}):
            self.assertEqual(self._observe(ADMIN, "/admin"), ([], [], []))


if __name__ == "__main__":
    unittest.main()
