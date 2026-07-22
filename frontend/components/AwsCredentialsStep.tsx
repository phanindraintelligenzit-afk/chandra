import { KeyRound, MapPin, ShieldCheck, User } from "lucide-react";

type AwsCredentialsStepProps = {
  awsRegion: string;
  setAwsRegion: (v: string) => void;
  awsAccessKey: string;
  setAwsAccessKey: (v: string) => void;
  awsSecretKey: string;
  setAwsSecretKey: (v: string) => void;
  awsSyntheticAccountId: string;
  setAwsSyntheticAccountId: (v: string) => void;
};

export default function AwsCredentialsStep({
  awsRegion,
  setAwsRegion,
  awsAccessKey,
  setAwsAccessKey,
  awsSecretKey,
  setAwsSecretKey,
  awsSyntheticAccountId,
  setAwsSyntheticAccountId
}: AwsCredentialsStepProps) {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-3xl font-semibold tracking-[0.02em]">AWS Identity Configuration</h2>
        <p className="mt-1 text-sm text-frost/60">Configure access credentials for your AWS Cloud Engineer role.</p>
      </div>

      <div className="space-y-4 pt-2">
        <div>
          <label className="text-[0.65rem] font-semibold uppercase tracking-[0.1em] text-frost/70">
            AWS Default Region
          </label>
          <div className="relative mt-1">
            <div className="absolute inset-y-0 left-0 flex items-center pl-3">
              <MapPin className="h-4 w-4 text-frost/30" />
            </div>
            <input 
              type="text" 
              value={awsRegion}
              onChange={(e) => setAwsRegion(e.target.value)}
              className="w-full bg-black/40 border border-signal/20 rounded-xl py-3 pl-10 pr-4 text-sm text-frost placeholder-frost/20 focus:outline-none focus:border-amber/50 transition-colors"
            />
          </div>
        </div>

        <div>
          <label className="text-[0.65rem] font-semibold uppercase tracking-[0.1em] text-frost/70">
            AWS Access Key ID
          </label>
          <div className="relative mt-1">
            <div className="absolute inset-y-0 left-0 flex items-center pl-3">
              <User className="h-4 w-4 text-frost/30" />
            </div>
            <input 
              type="text" 
              value={awsAccessKey}
              onChange={(e) => setAwsAccessKey(e.target.value)}
              className="w-full bg-black/40 border border-signal/20 rounded-xl py-3 pl-10 pr-4 text-sm text-frost placeholder-frost/20 focus:outline-none focus:border-amber/50 transition-colors"
            />
          </div>
        </div>

        <div>
          <label className="text-[0.65rem] font-semibold uppercase tracking-[0.1em] text-frost/70">
            AWS Secret Access Key
          </label>
          <div className="relative mt-1">
            <div className="absolute inset-y-0 left-0 flex items-center pl-3">
              <KeyRound className="h-4 w-4 text-frost/30" />
            </div>
            <input 
              type="password" 
              value={awsSecretKey}
              onChange={(e) => setAwsSecretKey(e.target.value)}
              className="w-full bg-black/40 border border-signal/20 rounded-xl py-3 pl-10 pr-4 text-sm text-frost placeholder-frost/20 focus:outline-none focus:border-amber/50 transition-colors"
            />
          </div>
        </div>

        <div>
          <label className="text-[0.65rem] font-semibold uppercase tracking-[0.1em] text-frost/70">
            Synthetic Account ID
          </label>
          <div className="relative mt-1">
            <div className="absolute inset-y-0 left-0 flex items-center pl-3">
              <ShieldCheck className="h-4 w-4 text-frost/30" />
            </div>
            <input 
              type="text" 
              value={awsSyntheticAccountId}
              onChange={(e) => setAwsSyntheticAccountId(e.target.value)}
              className="w-full bg-black/40 border border-signal/20 rounded-xl py-3 pl-10 pr-4 text-sm text-frost placeholder-frost/20 focus:outline-none focus:border-amber/50 transition-colors"
            />
          </div>
        </div>
      </div>
    </div>
  );
}
