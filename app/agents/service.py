from __future__ import annotations

from app.agents.registry import default_agents
from app.core.database import Database
from app.core.errors import AuthorizationError, NotFoundError
from app.core.utils import json_dumps, json_loads, new_id, utcnow_iso
from app.schemas.agents import AgentDefinition, AgentRole, AgentRunRecord, HandoffRecord


class AgentService:
    def __init__(self, database: Database):
        self.database = database
        self._seed_registry()

    def list_agents(self) -> list[AgentDefinition]:
        rows = self.database.fetch_all("SELECT * FROM agents ORDER BY name")
        return [
            AgentDefinition(
                id=row["id"],
                name=row["name"],
                role=row["role"],
                description=row["description"],
                provider_profile=row["provider_profile"],
                allowed_connectors=json_loads(row["allowed_connectors_json"], []),
                capabilities=json_loads(row["capabilities_json"], []),
                memory_namespace=row["memory_namespace"],
            )
            for row in rows
        ]

    def get_by_role(self, role: AgentRole) -> AgentDefinition:
        row = self.database.fetch_one("SELECT * FROM agents WHERE role = ? LIMIT 1", (role.value,))
        if row is None:
            raise NotFoundError(f"Agent with role {role.value} not found.")
        return AgentDefinition(
            id=row["id"],
            name=row["name"],
            role=row["role"],
            description=row["description"],
            provider_profile=row["provider_profile"],
            allowed_connectors=json_loads(row["allowed_connectors_json"], []),
            capabilities=json_loads(row["capabilities_json"], []),
            memory_namespace=row["memory_namespace"],
        )

    def start_run(
        self,
        run_id: str,
        agent: AgentDefinition,
        *,
        input_payload: dict,
        provider_profile: str | None = None,
        parent_agent_run_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AgentRunRecord:
        record = AgentRunRecord(
            id=new_id("agentrun"),
            run_id=run_id,
            agent_id=agent.id,
            agent_name=agent.name,
            role=agent.role,
            status="running",
            provider_profile=provider_profile or agent.provider_profile,
            provider_name=None,
            input=input_payload,
            output={},
            parent_agent_run_id=parent_agent_run_id,
            correlation_id=correlation_id,
            started_at=utcnow_iso(),
            completed_at=None,
        )
        self.database.execute(
            """
            INSERT INTO agent_runs(
                id, run_id, agent_id, agent_name, role, status, provider_profile, provider_name,
                input_json, output_json, parent_agent_run_id, correlation_id, started_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.run_id,
                record.agent_id,
                record.agent_name,
                record.role.value,
                record.status,
                record.provider_profile,
                record.provider_name,
                json_dumps(record.input),
                json_dumps(record.output),
                record.parent_agent_run_id,
                record.correlation_id,
                record.started_at,
                record.completed_at,
            ),
        )
        return record

    def complete_run(
        self,
        agent_run_id: str,
        *,
        status: str,
        output_payload: dict,
        provider_name: str | None = None,
    ) -> AgentRunRecord:
        completed_at = utcnow_iso()
        self.database.execute(
            """
            UPDATE agent_runs
            SET status = ?, output_json = ?, provider_name = ?, completed_at = ?
            WHERE id = ?
            """,
            (status, json_dumps(output_payload), provider_name, completed_at, agent_run_id),
        )
        row = self.database.fetch_one("SELECT * FROM agent_runs WHERE id = ?", (agent_run_id,))
        return self._row_to_run(row)

    def create_handoff(
        self,
        run_id: str,
        *,
        from_agent_run_id: str | None,
        to_agent: AgentDefinition,
        title: str,
        payload: dict,
        correlation_id: str | None = None,
    ) -> HandoffRecord:
        record = HandoffRecord(
            id=new_id("handoff"),
            run_id=run_id,
            from_agent_run_id=from_agent_run_id,
            to_agent_id=to_agent.id,
            to_agent_role=to_agent.role,
            title=title,
            payload=payload,
            status="pending",
            correlation_id=correlation_id,
            created_at=utcnow_iso(),
            completed_at=None,
        )
        self.database.execute(
            """
            INSERT INTO handoffs(
                id, run_id, from_agent_run_id, to_agent_id, to_agent_role, title,
                payload_json, status, correlation_id, created_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.run_id,
                record.from_agent_run_id,
                record.to_agent_id,
                record.to_agent_role.value,
                record.title,
                json_dumps(record.payload),
                record.status,
                record.correlation_id,
                record.created_at,
                record.completed_at,
            ),
        )
        return record

    def complete_handoff(self, handoff_id: str, status: str = "completed") -> HandoffRecord:
        completed_at = utcnow_iso()
        self.database.execute(
            "UPDATE handoffs SET status = ?, completed_at = ? WHERE id = ?",
            (status, completed_at, handoff_id),
        )
        row = self.database.fetch_one("SELECT * FROM handoffs WHERE id = ?", (handoff_id,))
        return self._row_to_handoff(row)

    def list_run_history(self, run_id: str) -> list[AgentRunRecord]:
        rows = self.database.fetch_all(
            "SELECT * FROM agent_runs WHERE run_id = ? ORDER BY started_at ASC",
            (run_id,),
        )
        return [self._row_to_run(row) for row in rows]

    def list_handoffs(self, run_id: str) -> list[HandoffRecord]:
        rows = self.database.fetch_all(
            "SELECT * FROM handoffs WHERE run_id = ? ORDER BY created_at ASC",
            (run_id,),
        )
        return [self._row_to_handoff(row) for row in rows]

    def recent_runs(self, limit: int = 20) -> list[dict]:
        rows = self.database.fetch_all(
            """
            SELECT run_id, COUNT(*) AS step_count, MAX(completed_at) AS last_completed, MAX(started_at) AS started_at
            FROM agent_runs
            GROUP BY run_id
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    @staticmethod
    def assert_connector_allowed(agent: AgentDefinition, connector: str) -> None:
        if connector not in agent.allowed_connectors:
            raise AuthorizationError(
                f"{agent.name} is not allowed to use connector {connector}."
            )

    @staticmethod
    def assert_capability(agent: AgentDefinition, capability: str) -> None:
        if capability not in agent.capabilities:
            raise AuthorizationError(
                f"{agent.name} does not have capability {capability}."
            )

    def _seed_registry(self) -> None:
        for agent in default_agents():
            self.database.execute(
                """
                INSERT INTO agents(
                    id, name, role, description, provider_profile, allowed_connectors_json,
                    capabilities_json, memory_namespace, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    role = excluded.role,
                    description = excluded.description,
                    provider_profile = excluded.provider_profile,
                    allowed_connectors_json = excluded.allowed_connectors_json,
                    capabilities_json = excluded.capabilities_json,
                    memory_namespace = excluded.memory_namespace
                """,
                (
                    agent.id,
                    agent.name,
                    agent.role.value,
                    agent.description,
                    agent.provider_profile,
                    json_dumps(agent.allowed_connectors),
                    json_dumps(agent.capabilities),
                    agent.memory_namespace,
                    utcnow_iso(),
                ),
            )

    @staticmethod
    def _row_to_run(row) -> AgentRunRecord:
        return AgentRunRecord(
            id=row["id"],
            run_id=row["run_id"],
            agent_id=row["agent_id"],
            agent_name=row["agent_name"],
            role=row["role"],
            status=row["status"],
            provider_profile=row["provider_profile"],
            provider_name=row["provider_name"],
            input=json_loads(row["input_json"], {}),
            output=json_loads(row["output_json"], {}),
            parent_agent_run_id=row["parent_agent_run_id"],
            correlation_id=row["correlation_id"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _row_to_handoff(row) -> HandoffRecord:
        return HandoffRecord(
            id=row["id"],
            run_id=row["run_id"],
            from_agent_run_id=row["from_agent_run_id"],
            to_agent_id=row["to_agent_id"],
            to_agent_role=row["to_agent_role"],
            title=row["title"],
            payload=json_loads(row["payload_json"], {}),
            status=row["status"],
            correlation_id=row["correlation_id"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )
