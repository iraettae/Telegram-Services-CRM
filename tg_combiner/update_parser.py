import sys

with open('modules/parser.py', 'r') as f:
    content = f.read()

import re

old_def = """    async def analyze_chat(self, chat_id, message_thread_id=None, progress_callback=None) -> list[dict]:"""

# We find the start index
start_idx = content.find(old_def)
if start_idx == -1:
    print("Could not find analyze_chat definition")
    sys.exit(1)

# We find the end index (end of the method, before export_results)
end_str = "    async def export_results(self, results: list[dict], filename: str) -> str:"
end_idx = content.find(end_str, start_idx)
if end_idx == -1:
    print("Could not find end of analyze_chat")
    sys.exit(1)

# New definition
new_code = """    async def _process_collected_data(
        self, chat_id, users, user_messages, total_users, progress_callback
    ) -> list[dict]:
        \"\"\"Process collected user data: filter, enrich bio, batch AI analysis, slang fallback, avatars.\"\"\"
        # Filter out users with < 2 messages
        active_users = {uid: info for uid, info in users.items() if len(user_messages.get(uid, [])) >= 2}
        skipped = total_users - len(active_users)
        if skipped:
            logger.info(f"SmartParser: skipped {skipped} users with < 2 messages")

        if not active_users:
            logger.info(f"SmartParser: no active users (with 2+ messages) in {chat_id}")
            return []

        # Step 1.5: Enrich with bio/status
        logger.info(f"SmartParser: enriching {len(active_users)} users with bio/status...")
        bio_data = await self._enrich_users_bio(list(active_users.keys()))

        # Step 2: Form batches and send to AI
        batch_users_list = []
        idx = 0
        for user_id, info in active_users.items():
            idx += 1
            bio_info = bio_data.get(user_id, {})
            batch_users_list.append({
                "idx": idx,
                "user_id": user_id,
                "name": info["name"],
                "username": info["username"],
                "photo_id": info.get("photo_id"),
                "bio": bio_info.get("bio", ""),
                "status": bio_info.get("status", ""),
                "is_premium": bio_info.get("is_premium", False),
                "messages": user_messages.get(user_id, []),
            })

        # Split into batches of BATCH_SIZE
        batches = []
        for i in range(0, len(batch_users_list), self.BATCH_SIZE):
            batches.append(batch_users_list[i:i + self.BATCH_SIZE])

        logger.info(f"SmartParser: {len(active_users)} users → {len(batches)} batch(es) of max {self.BATCH_SIZE}")

        all_ai_targets = []
        api_failed = False

        for batch_idx, batch in enumerate(batches):
            prompt = self._build_batch_prompt(batch)
            logger.info(f"SmartParser: sending batch {batch_idx+1}/{len(batches)} ({len(batch)} users, ~{len(prompt)} chars)")
            
            targets = await self._call_api_with_rate_limit(prompt, request_num=batch_idx)
            
            if targets is None:
                logger.error(f"SmartParser: batch {batch_idx+1} failed (API error)")
                api_failed = True
                break
            
            all_ai_targets.extend(targets)
            
            if progress_callback:
                processed_so_far = min((batch_idx + 1) * self.BATCH_SIZE, len(active_users))
                await progress_callback(processed_so_far, total_users, len(all_ai_targets))

        # Step 3: Map AI results back to user data
        results = []
        ai_target_user_ids = set()
        idx_to_user = {u["idx"]: u for u in batch_users_list}

        for target in all_ai_targets:
            t_user_id = target.get("user_id")
            t_idx = target.get("idx")
            
            user_info = None
            if t_user_id and t_user_id in active_users:
                user_info = active_users[t_user_id]
                uid = t_user_id
            elif t_idx and t_idx in idx_to_user:
                uid = idx_to_user[t_idx]["user_id"]
                user_info = active_users.get(uid)
            
            if not user_info:
                continue

            confidence = target.get("confidence", 50)
            if confidence < 40:
                continue

            ai_target_user_ids.add(uid)
            results.append({
                "user_id": uid,
                "display_name": user_info["name"],
                "username": user_info["username"],
                "is_target": True,
                "confidence": confidence,
                "reason": target.get("reason", ""),
                "inferred_age": target.get("inferred_age"),
                "inferred_city": target.get("inferred_city"),
                "phase": "batch",
            })

        # Step 4: Slang fallback
        slang_found = 0
        for user_id, info in active_users.items():
            if user_id in ai_target_user_ids:
                continue
            msgs = user_messages.get(user_id, [])
            slang_match, slang_score = self._has_courier_slang(msgs, threshold=self.config.slang_threshold)
            if slang_match:
                slang_found += 1
                results.append({
                    "user_id": user_id,
                    "display_name": info["name"],
                    "username": info["username"],
                    "is_target": True,
                    "confidence": min(60 + slang_score * 2, 90),
                    "reason": f"Обнаружен курьерский сленг (score={slang_score}, порог={self.config.slang_threshold})",
                    "inferred_age": None,
                    "inferred_city": None,
                    "phase": "slang_fallback",
                })
        
        if slang_found:
            logger.info(f"SmartParser: slang fallback added {slang_found} more targets")

        # Step 5: Download avatars in parallel
        import os
        import asyncio
        from datetime import datetime
        import json
        avatar_sem = asyncio.Semaphore(10)
        
        async def _download_avatar(result_item):
            user_id = result_item["user_id"]
            info = active_users.get(user_id, {})
            avatar_url = None
            if info.get("photo_id"):
                async with avatar_sem:
                    try:
                        save_path = f"webapp/static/avatars/{user_id}.jpg"
                        if not os.path.exists(save_path):
                            await self.client.download_media(
                                info["photo_id"],
                                file_name=save_path
                            )
                        if os.path.exists(save_path):
                            avatar_url = f"/static/avatars/{user_id}.jpg"
                    except Exception as e:
                        logger.error(f"Error downloading avatar for {user_id}: {e}")
            result_item["avatar_url"] = avatar_url
            result_item["parsed_at"] = datetime.now().isoformat()
        
        logger.info(f"SmartParser: downloading avatars for {len(results)} targets (parallel, sem=10)...")
        await asyncio.gather(*[_download_avatar(r) for r in results])

        await self.close()

        # Save to contacts.json
        if results:
            try:
                db_path = "contacts.json"
                existing = []
                if os.path.exists(db_path):
                    with open(db_path, "r", encoding="utf-8") as f:
                        try:
                            existing = json.load(f)
                        except json.JSONDecodeError:
                            existing = []
                
                existing_map = {str(r_item.get("user_id")): r_item for r_item in existing if r_item.get("user_id")}
                for r_item in results:
                    existing_map[str(r_item["user_id"])] = r_item
                
                with open(db_path, "w", encoding="utf-8") as f:
                    json.dump(list(existing_map.values()), f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"Failed to save contacts.json: {e}")

        if progress_callback:
            await progress_callback(total_users, total_users, len(results))

        logger.info(f"SmartParser: analysis complete. {len(results)}/{total_users} targets found.")
        return results

    async def analyze_chat(self, chat_id, message_thread_id=None, progress_callback=None) -> list[dict]:
        \"\"\"
        Analyze all users in a chat using mega-batch approach.
        Collects all messages in a single pass, then sends 1-2 large batches to AI.
        For forum chats (with topics), auto-detects and scans all topics.
        \"\"\"
        # Step 0: Auto-detect forum chats and scan all topics
        if not message_thread_id:
            try:
                chat_obj = await self.client.get_chat(chat_id)
                is_forum = getattr(chat_obj, 'is_forum', False)
                if is_forum:
                    logger.info(f"SmartParser: '{chat_id}' is a forum chat. Auto-scanning all topics...")
                    # Get topics via raw API
                    from pyrogram.raw.functions.channels import GetForumTopics
                    from pyrogram.raw.types import ForumTopic
                    
                    peer = await self.client.resolve_peer(chat_id)
                    
                    all_users = {}
                    all_user_messages = {}
                    topics_scanned = 0
                    
                    try:
                        result = await self.client.invoke(
                            GetForumTopics(
                                channel=peer,
                                offset_date=0,
                                offset_id=0,
                                offset_topic=0,
                                limit=100,
                                q=""
                            )
                        )
                        
                        topic_ids = []
                        for topic in getattr(result, 'topics', []):
                            if isinstance(topic, ForumTopic):
                                topic_ids.append(topic.id)
                        
                        logger.info(f"SmartParser: found {len(topic_ids)} topics in forum '{chat_id}'")
                        
                        for tid in topic_ids:
                            try:
                                t_users, t_msgs = await self._collect_chat_data(chat_id, message_thread_id=tid)
                                # Merge results
                                for uid, info in t_users.items():
                                    if uid not in all_users:
                                        all_users[uid] = info
                                        all_user_messages[uid] = []
                                    all_user_messages[uid].extend(t_msgs.get(uid, []))
                                    # Cap at 30 messages per user across all topics
                                    all_user_messages[uid] = all_user_messages[uid][:30]
                                topics_scanned += 1
                                logger.info(
                                    f"SmartParser: topic {tid} → {len(t_users)} users, "
                                    f"{sum(len(v) for v in t_msgs.values())} msgs"
                                )
                            except Exception as topic_err:
                                logger.warning(f"SmartParser: error scanning topic {tid}: {topic_err}")
                                continue
                    except Exception as e:
                        logger.error(f"SmartParser: failed to get forum topics: {e}", exc_info=True)
                        # Fallback: try General topic (id=1)
                        logger.info("SmartParser: falling back to General topic (id=1)")
                        all_users, all_user_messages = await self._collect_chat_data(chat_id, message_thread_id=1)
                    
                    logger.info(
                        f"SmartParser: forum scan complete. "
                        f"Topics scanned: {topics_scanned}, "
                        f"Total users: {len(all_users)}, "
                        f"Total messages: {sum(len(v) for v in all_user_messages.values())}"
                    )
                    
                    # Use the merged data instead of calling _collect_chat_data again
                    users = all_users
                    user_messages = all_user_messages
                    
                    # Skip the regular _collect_chat_data call below
                    # Jump to the rest of analyze_chat with users and user_messages already set
                    total_users = len(users)
                    if total_users == 0:
                        logger.info(f"SmartParser: no users found in forum '{chat_id}'")
                        return []
                    
                    if progress_callback:
                        await progress_callback(0, total_users, 0)
                    
                    # Continue with the rest of the method from "Filter out users..."
                    # We need to skip the regular collection below
                    return await self._process_collected_data(
                        chat_id, users, user_messages, total_users, progress_callback
                    )
            except Exception as forum_err:
                logger.warning(f"SmartParser: forum detection failed for '{chat_id}': {forum_err}")
                # Continue with normal flow

        # Step 1: Collect all users and their messages in a single pass
        if progress_callback:
            await progress_callback(0, 0, 0)

        users, user_messages = await self._collect_chat_data(chat_id, message_thread_id)
        total_users = len(users)

        if total_users == 0:
            logger.info(f"SmartParser: no users found in {chat_id}")
            return []

        if progress_callback:
            await progress_callback(0, total_users, 0)

        return await self._process_collected_data(
            chat_id, users, user_messages, total_users, progress_callback
        )
"""

final_content = content[:start_idx] + new_code + "\n" + content[end_idx:]

with open('modules/parser.py', 'w') as f:
    f.write(final_content)

print("Successfully updated modules/parser.py")
