import { useState } from "react";
import { toast } from "sonner";

import { Button } from "~/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "~/components/ui/dialog";
import { Input } from "~/components/ui/input";
import { Label } from "~/components/ui/label";
import { useCreateModel } from "~/features/admin/llm/hooks";

interface AddModelDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  providerId: string;
  providerType: string;
}

export const AddModelDialog = ({ open, onOpenChange, providerId, providerType }: AddModelDialogProps) => {
  const [name, setName] = useState("");
  const [modelId, setModelId] = useState("");
  const [maxTokens, setMaxTokens] = useState("");
  const [endpointSuffix, setEndpointSuffix] = useState("");

  const createModel = useCreateModel();

  const isBeijingBank = providerType === "beijingbank";

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      toast.error("请输入模型名称");
      return;
    }
    if (!modelId.trim()) {
      toast.error("请输入模型 ID");
      return;
    }

    try {
      const limits: Record<string, unknown> = {};
      if (maxTokens.trim()) {
        const parsed = parseInt(maxTokens, 10);
        if (!isNaN(parsed) && parsed > 0) {
          limits.max_tokens = parsed;
        }
      }

      const defaults: Record<string, unknown> = {};
      if (isBeijingBank && endpointSuffix.trim()) {
        defaults.endpoint_suffix = endpointSuffix.trim();
      }

      await createModel.mutateAsync({
        provider_id: providerId,
        name: name.trim(),
        model_id: modelId.trim(),
        limits,
        defaults: Object.keys(defaults).length > 0 ? defaults : undefined,
      });
      toast.success("模型创建成功");
      setName("");
      setModelId("");
      setMaxTokens("");
      setEndpointSuffix("");
      onOpenChange(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "创建失败");
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>添加模型</DialogTitle>
            <DialogDescription>为当前 Provider 添加一个新模型</DialogDescription>
          </DialogHeader>

          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="model-name">名称</Label>
              <Input
                id="model-name"
                placeholder="例如: GPT-4o"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
              />
            </div>

            <div className="grid gap-2">
              <Label htmlFor="model-id">模型 ID</Label>
              <Input
                id="model-id"
                placeholder="例如: gpt-4o"
                value={modelId}
                onChange={(e) => setModelId(e.target.value)}
              />
              <p className="text-muted-foreground text-xs">
                API 调用时使用的模型标识符
              </p>
            </div>

            <div className="grid gap-2">
              <Label htmlFor="model-max-tokens">Max Tokens</Label>
              <Input
                id="model-max-tokens"
                type="number"
                placeholder="例如: 128000"
                value={maxTokens}
                onChange={(e) => setMaxTokens(e.target.value)}
              />
              <p className="text-muted-foreground text-xs">
                可选，模型最大 Token 数
              </p>
            </div>

            {isBeijingBank && (
              <div className="grid gap-2">
                <Label htmlFor="endpoint-suffix">Endpoint Suffix</Label>
                <Input
                  id="endpoint-suffix"
                  placeholder="例如: qwen3-30b-a"
                  value={endpointSuffix}
                  onChange={(e) => setEndpointSuffix(e.target.value)}
                />
                <p className="text-muted-foreground text-xs">
                  URL 路径后缀，用于构建 API 地址。如未填写则使用模型 ID
                </p>
              </div>
            )}
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              取消
            </Button>
            <Button type="submit" disabled={createModel.isPending}>
              {createModel.isPending ? "创建中..." : "创建"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
};
