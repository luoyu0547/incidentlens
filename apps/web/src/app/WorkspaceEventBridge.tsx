import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  connectWorkspaceEvents,
  type WorkspaceEventStatus,
  type WorkspaceResourceEvent,
} from '@incidentlens/protocol';
import { queryKeys } from '../api/query-keys';

export function WorkspaceEventBridge() {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<WorkspaceEventStatus>('connecting');
  useEffect(() => {
    const invalidate = (event: WorkspaceResourceEvent) => {
      switch (event.resource_kind) {
        case 'overview': void queryClient.invalidateQueries({ queryKey: queryKeys.overview }); break;
        case 'target': void queryClient.invalidateQueries({ queryKey: queryKeys.targets }); break;
        case 'service':
          if (event.resource_id) void queryClient.invalidateQueries({ queryKey: queryKeys.service(event.resource_id) });
          if (event.target_id) void queryClient.invalidateQueries({ queryKey: queryKeys.targetServices(event.target_id) });
          break;
        case 'issue':
          if (event.resource_id) void queryClient.invalidateQueries({ queryKey: queryKeys.issue(event.resource_id) });
          void queryClient.invalidateQueries({ queryKey: ['issues'] }); break;
        case 'investigation':
          if (event.resource_id) void queryClient.invalidateQueries({ queryKey: queryKeys.investigation(event.resource_id) });
          void queryClient.invalidateQueries({ queryKey: ['investigations'] }); break;
        case 'evidence':
          if (event.resource_id) void queryClient.invalidateQueries({ queryKey: queryKeys.evidence(event.resource_id) }); break;
      }
    };
    const connection = connectWorkspaceEvents({
      onResourceChanged: invalidate,
      onGap: () => { void queryClient.invalidateQueries(); },
      onStatus: setStatus,
    });
    return () => connection.close();
  }, [queryClient]);
  return <span role="status" data-workspace-status={status}>{status === 'reconnecting' ? '工作区正在重新同步' : status === 'live' ? '工作区已连接' : status === 'authentication-error' ? '工作区认证失败' : status === 'closed' ? '工作区连接已关闭' : '工作区连接中'}</span>;
}
