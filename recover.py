"""Recover the bot's SQLite database from a Telegram Desktop JSON export.

By default this only writes an audit report. --write-db creates a NEW database;
existing files, including poker.db, are never replaced.
"""

import argparse
from collections import defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


DATE = r"\d{1,2}\.\d{1,2}\.\d{4}"
HEADER = re.compile(rf"(?:(?:Результаты|Итоги)\s+)?({DATE})\s*:?$", re.I)
DELETED = re.compile(rf"Результаты за ({DATE}) удалены 🗑(?:\ufe0f)?(?:\n.*)?\Z", re.S)
ROW = re.compile(r"(.+?)(?:\s*([+\-−]\s*\d+)|\s+(\d+))\s*\Z")
STAT_ROW = re.compile(r"(.+?)\s+([+\-]?\d+)(?:\s+\((\d+) игр\))?\Z")


def plain(value):
    """Telegram's text is either a string or a mixed list of entities/strings."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(plain(part) for part in value)
    if isinstance(value, dict):
        return plain(value.get("text", ""))
    raise ValueError("Неподдерживаемое поле text в экспорте Telegram")


def iso_date(value):
    return datetime.strptime(value, "%d.%m.%Y").date().isoformat()


def identity(name):
    return name.casefold() if name.startswith("@") else name


def canonical(name, aliases):
    name = " ".join(name.split())
    seen = set()
    lookup = {identity(k): v for k, v in aliases.items()}
    while identity(name) in lookup:
        if identity(name) in seen:
            raise ValueError("Цикл в aliases: " + name)
        seen.add(identity(name))
        target = lookup[identity(name)]
        if identity(target) == identity(name):
            return target
        name = target
    return name


def load_chat(path, chat_id=None):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    chats = [data] if "messages" in data else data.get("chats", {}).get("list", [])
    if chat_id is not None:
        chats = [c for c in chats if int(c["id"]) == chat_id or telegram_id(c) == chat_id]
    if len(chats) != 1:
        choices = ", ".join(f'{c.get("id")}: {c.get("name")}' for c in chats)
        raise ValueError("Выберите один чат через --chat-id. Чаты: " + choices)
    chat = chats[0]
    telegram_id(chat)  # Refuse ambiguous chat types rather than guess a group ID.
    messages = chat.get("messages")
    if not isinstance(messages, list):
        raise ValueError("В экспорте нет массива messages")
    ids = [m["id"] for m in messages]
    if any(type(i) is not int for i in ids) or len(ids) != len(set(ids)):
        raise ValueError("ID сообщений должны быть целыми и уникальными внутри чата")
    return chat


def telegram_id(chat):
    bare = int(chat["id"])
    kind = chat.get("type")
    if kind in ("private_supergroup", "public_supergroup", "private_channel", "public_channel"):
        return bare if bare < 0 else -(1000000000000 + bare)
    if kind in ("private_group", "public_group"):
        return -abs(bare)
    raise ValueError("Нужен экспорт группы/канала, получен тип: " + str(kind))


def parse_report(message):
    text = plain(message.get("text", "")).strip()
    lines = text.splitlines()
    if not lines:
        return None
    header = HEADER.fullmatch(lines[0].strip())
    if not header:
        return None
    # A photo caption with a date is handled as media, not as an empty game.
    if len(lines) == 1 and message.get("photo"):
        return None
    result = {
        "message_id": message["id"], "sent_at": message.get("date"),
        "sender_id": message.get("from_id"), "text": text,
        "date": None, "rows": [], "errors": [], "warnings": [], "notes": [],
        "sources": [{"message_id": message["id"], "kind": "text"}],
    }
    try:
        result["date"] = iso_date(header[1])
    except ValueError:
        result["errors"].append("Некорректная дата игры")
    for index, line in enumerate(lines[1:], 2):
        line = line.strip()
        if not line:
            continue
        if line.startswith(("П.С.", "P.S.", "PS:", "Примечание:")):
            result["notes"].append("\n".join(lines[index - 1:]))
            break
        match = ROW.fullmatch(line)
        if not match:
            result["errors"].append(f"Строка {index} не разобрана: {line}")
            continue
        amount = int(re.sub(r"\s", "", match[2] or match[3]).replace("−", "-"))
        if not -(2 ** 63) <= amount < 2 ** 63:
            result["errors"].append(f"Сумма вне диапазона SQLite: {line}")
            continue
        result["rows"].append({"name": match[1].strip(), "amount": amount})
    if not result["rows"]:
        result["errors"].append("В отчёте нет результатов игроков")
    if message.get("edited"):
        result["warnings"].append("Доступна только последняя редакция сообщения")
    if message.get("forwarded_from"):
        result["warnings"].append("Пересланное сообщение: проверьте принадлежность игры группе")
    return result


def totals(sessions):
    output = {}
    for game in sessions:
        for row in game["rows"]:
            key = identity(row["name"])
            item = output.setdefault(key, {"name": row["name"], "amount": 0, "games": 0})
            item["amount"] += row["amount"]
            item["games"] += 1
    return output


def analyze(chat, config):
    aliases = config.get("aliases", {})
    ignored = set(config.get("ignore_message_ids", []))
    # Migrated basic-group messages can have negative IDs. Dates preserve order.
    messages = sorted(chat["messages"], key=lambda m: (m.get("date", ""), m["id"]))
    positions = {m["id"]: i for i, m in enumerate(messages)}
    by_id = {m["id"]: m for m in messages}
    reports = {}
    warnings = []
    errors = []
    for message in messages:
        report = parse_report(message) if message.get("type") == "message" else None
        if report:
            reports[message["id"]] = report
    for missing in ignored - by_id.keys():
        errors.append(f"ignore_message_ids: в истории нет сообщения {missing}")
    bot_id = config.get("bot_sender_id")
    if not bot_id:
        senders = {
            m.get("from_id") for m in messages
            if plain(m.get("text", "")).strip() == "Результаты сохранены ✅"
            and m.get("reply_to_message_id") in reports and m.get("from_id")
        }
        if len(senders) == 1:
            bot_id = senders.pop()
        elif len(senders) > 1:
            errors.append("Несколько отправителей подтверждений: укажите bot_sender_id")
    confirmations = defaultdict(list)
    for m in messages:
        if bot_id and m.get("from_id") == bot_id and plain(m.get("text", "")).strip() == "Результаты сохранены ✅":
            confirmations[m.get("reply_to_message_id")].append(m["id"])
    active = defaultdict(list)
    cancellations = []
    for m in messages:
        report = reports.get(m["id"])
        if report:
            report["confirmation_ids"] = confirmations.get(m["id"], [])
            report["status"] = "ignored" if m["id"] in ignored else "candidate"
            if report["status"] != "ignored":
                active[report["date"]].append(report)
        deleted = DELETED.fullmatch(plain(m.get("text", "")).strip())
        if deleted and bot_id and m.get("from_id") == bot_id:
            date = iso_date(deleted[1])
            removed = active.pop(date, [])
            for old in removed:
                old["status"] = "cancelled"
            cancellations.append({"message_id": m["id"], "date": date,
                                  "removed_message_ids": [r["message_id"] for r in removed]})
    sessions = []
    selections = config.get("select_by_date", {})
    for date, candidates in active.items():
        if date in selections:
            choice = selections[date]
            chosen = [r for r in candidates if r["message_id"] == choice]
            if choice is None:
                for r in candidates:
                    r["status"] = "excluded_by_decision"
                continue
            if not chosen:
                errors.append(f"Выбранное сообщение {choice} отсутствует среди активных отчётов за {date}")
                continue
        else:
            chosen = candidates
        for candidate in chosen:
            local_aliases = dict(aliases)
            local_aliases.update(config.get("message_aliases", {}).get(str(candidate["message_id"]), {}))
            for row in candidate["rows"]:
                row["original_name"] = row["name"]
                row["name"] = canonical(row["name"], local_aliases)
            keys = [identity(r["name"]) for r in candidate["rows"]]
            if len(keys) != len(set(keys)):
                candidate["errors"].append("Игрок повторяется после сопоставления имён")
        invalid = [c for c in chosen if c["errors"]]
        if invalid:
            for candidate in invalid:
                candidate["status"] = "invalid"
                errors.extend(f"Сообщение {candidate['message_id']}: {e}" for e in candidate["errors"])
            continue
        signatures = {tuple(sorted((identity(r["name"]), r["amount"]) for r in c["rows"])) for c in chosen}
        if len(signatures) > 1:
            errors.append(f"Разные отчёты за {date}: {[r['message_id'] for r in chosen]}; задайте select_by_date")
            continue
        selected = next((r for r in chosen if r["confirmation_ids"]), chosen[0])
        for r in candidates:
            r["status"] = "selected" if r is selected else "duplicate"
        sessions.append(selected)
    for supplement in config.get("supplemental_sessions", []):
        game = json.loads(json.dumps(supplement))
        datetime.strptime(game["date"], "%Y-%m-%d")
        if any(g["date"] == game["date"] for g in sessions):
            errors.append("Дополнение дублирует игру за " + game["date"])
            continue
        if not game.get("sources") or game.get("recorded_at_message_id") not in positions:
            errors.append("Дополнение требует sources и recorded_at_message_id: " + game["date"])
            continue
        game.setdefault("warnings", [])
        game.setdefault("notes", [])
        for row in game["rows"]:
            row["original_name"] = row["name"]
            row["name"] = canonical(row["name"], aliases)
        keys = [identity(r["name"]) for r in game["rows"]]
        if not keys or len(keys) != len(set(keys)) or any(type(r["amount"]) is not int or not -(2 ** 63) <= r["amount"] < 2 ** 63 for r in game["rows"]):
            errors.append("Некорректные строки дополнения за " + game["date"])
            continue
        game["status"] = "supplemental"
        sessions.append(game)
    for game in sessions:
        game["balance"] = sum(r["amount"] for r in game["rows"])
        if game["balance"]:
            game["warnings"].append(f"Сумма результатов не равна нулю: {game['balance']:+}")
        warnings.extend(f"{game['date']}: {w}" for w in game["warnings"])
    validations = []
    for checkpoint in config.get("validate_snapshots", []):
        snapshot = by_id[checkpoint]
        expected = {}
        for line in plain(snapshot.get("text", "")).splitlines()[1:]:
            match = STAT_ROW.fullmatch(line.strip())
            if not match:
                raise ValueError(f"Неразобранная строка контрольной статистики {checkpoint}: {line}")
            name = canonical(match[1], aliases)
            key = identity(name)
            if key in expected:
                raise ValueError("Контрольная статистика содержит повтор игрока: " + name)
            expected[key] = {"name": name, "amount": int(match[2]),
                             "games": int(match[3]) if match[3] else None}
        observed = totals([g for g in sessions if positions[g.get("message_id", g.get("recorded_at_message_id"))] <= positions[checkpoint]])
        differences = []
        for key in expected.keys() | observed.keys():
            want = expected.get(key, {"amount": 0, "games": 0})
            got = observed.get(key, {"amount": 0, "games": 0})
            if want["amount"] != got["amount"] or (want["games"] is not None and want["games"] != got["games"]):
                differences.append({"name": want.get("name", got.get("name")), "expected": want, "actual": got})
        validations.append({"message_id": checkpoint, "matched": not differences, "differences": differences})
        if differences:
            errors.append(f"Контрольная статистика {checkpoint}: расхождений {len(differences)}")
    media = [{"message_id": m["id"], "date": m.get("date"), "photo": m["photo"],
              "caption": plain(m.get("text", ""))} for m in messages if m.get("photo")]
    sessions.sort(key=lambda g: g["date"])
    player_totals = sorted(totals(sessions).values(), key=lambda r: (-r["amount"], r["name"]))
    return {
        "chat": {"name": chat.get("name"), "export_id": chat["id"], "tg_id": telegram_id(chat)},
        "scope": config.get("scope"),
        "alias_evidence": config.get("alias_evidence", []),
        "bot_sender_id": bot_id,
        "summary": {"messages": len(messages), "text_reports": len(reports), "sessions": len(sessions),
                    "players": len(player_totals), "results": sum(len(g["rows"]) for g in sessions),
                    "cancellations": len(cancellations), "warnings": len(warnings), "errors": len(errors)},
        "errors": errors, "warnings": warnings, "cancellations": cancellations,
        "snapshot_validations": validations, "sessions": sessions, "player_totals": player_totals,
        "all_text_reports": list(reports.values()), "photos": media,
        "manual_review": config.get("manual_review", []),
    }


def create_database(report, target):
    if report["errors"]:
        raise ValueError("База не создана: сначала устраните ошибки из report.json")
    if not report["sessions"]:
        raise ValueError("База не создана: нет игр")
    if target.exists():
        raise FileExistsError("Существующий файл не будет заменён: " + str(target))
    # Importing models alone does not start the bot or touch its live database.
    from sqlalchemy import create_engine, event
    from sqlalchemy.engine import URL
    from sqlalchemy.orm import Session
    from app.models import Base, Group, Player, GroupPlayer, GameSession, PlayerResult

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".recover-", suffix=".db", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    engine = create_engine(URL.create("sqlite", database=str(temporary.resolve())))

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db, db.begin():
            group = Group(tg_id=report["chat"]["tg_id"], name=report["chat"]["name"])
            db.add(group)
            db.flush()
            players = {}
            for game in report["sessions"]:
                session = GameSession(group_id=group.id, date=datetime.strptime(game["date"], "%Y-%m-%d").date())
                db.add(session)
                db.flush()
                for row in game["rows"]:
                    key = identity(row["name"])
                    if key not in players:
                        player = Player(**{"username" if row["name"].startswith("@") else "full_name": row["name"]})
                        db.add(player)
                        db.flush()
                        db.add(GroupPlayer(group_id=group.id, player_id=player.id))
                        players[key] = player
                    db.add(PlayerResult(session_id=session.id, player_id=players[key].id, amount=row["amount"]))
        with engine.connect() as connection:
            if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
                raise ValueError("Не пройдена проверка целостности SQLite")
            if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Не пройдена проверка связей SQLite")
        engine.dispose()
        # Windows rename refuses to replace an existing destination. On POSIX,
        # link provides that guarantee (rename would overwrite the destination).
        # Both publish a complete, closed database without a partial copy.
        if os.name == "nt":
            temporary.rename(target.resolve())
        else:
            os.link(temporary, target)
    finally:
        engine.dispose()
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--chat-id", type=int)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("recovery"))
    parser.add_argument("--write-db", type=Path, help="Создать новый файл SQLite без перезаписи")
    args = parser.parse_args(argv)
    try:
        report_path = args.output_dir / "report.json"
        results_path = args.output_dir / "results.txt"
        stats_path = args.output_dir / "stats.txt"
        inputs = {p.resolve() for p in (args.export, args.config) if p is not None}
        outputs = [report_path, results_path, stats_path] + ([args.write_db] if args.write_db else [])
        if len({p.resolve() for p in outputs}) != len(outputs) or any(p.resolve() in inputs for p in outputs):
            raise ValueError("Пути входных данных и выходных файлов должны различаться")
        config = json.loads(args.config.read_text(encoding="utf-8-sig")) if args.config else {}
        report = analyze(load_chat(args.export, args.chat_id), config)
        report["source_sha256"] = hashlib.sha256(args.export.read_bytes()).hexdigest()
        report["config_sha256"] = hashlib.sha256(args.config.read_bytes()).hexdigest() if args.config else None
        args.output_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        results_path.write_text("\n\n".join(
            f"Результаты {datetime.strptime(g['date'], '%Y-%m-%d'):%d.%m.%Y}:\n" +
            "\n".join(f"{r['name']} {r['amount']:+}" for r in g["rows"]) for g in report["sessions"]
        ) + "\n", encoding="utf-8")
        stats_path.write_text("\n".join(f"{p['name']} {p['amount']:+} ({p['games']} игр)" for p in report["player_totals"]) + "\n", encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False))
        print("Аудит:", report_path.resolve())
        for error in report["errors"]:
            print("ОШИБКА:", error)
        if args.write_db:
            create_database(report, args.write_db)
            print("Создана база:", args.write_db.resolve())
        return 2 if report["errors"] else 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Ошибка:", error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
