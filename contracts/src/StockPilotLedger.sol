// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

/// @title StockPilotLedger
/// @notice On-chain ledger of paper-portfolio rebalances recorded by the
///         StockPilot agent (Base Stocks Maxxing Builder Quest, track
///         Agentic Stock Finance).
/// @dev SIMULATION ONLY: this contract records snapshots of a simulated
///      (paper) portfolio. It holds no real funds, performs no trading,
///      and moves no assets. Each rebalance stores hashes of the off-chain
///      NAV snapshot and the agent's rationale, plus the ticker list and
///      target weights in basis points (10000 bps = 100%).
contract StockPilotLedger {
    struct Rebalance {
        uint256 id;
        uint256 timestamp;
        bytes32 navSnapshotHash;
        bytes32 rationaleHash;
        string tickersCsv;
        uint256[] weightsBps;
        address agent;
    }

    event RebalanceRecorded(
        uint256 indexed id,
        address indexed agent,
        uint256 timestamp,
        bytes32 navSnapshotHash,
        bytes32 rationaleHash,
        string tickersCsv,
        uint256[] weightsBps
    );

    /// @notice Maximum number of holdings in a single rebalance.
    uint256 public constant MAX_HOLDINGS = 20;

    /// @notice Total weight, in basis points, that every rebalance must sum to.
    uint256 public constant TOTAL_BPS = 10000;

    address public owner;
    uint256 public rebalanceCount;
    mapping(uint256 => Rebalance) public rebalances;

    constructor() {
        owner = msg.sender;
    }

    /// @notice Record a new paper-portfolio rebalance. Only the owner (the
    ///         deployer / agent operator) may call this.
    /// @param navSnapshotHash keccak256 hash of the off-chain NAV snapshot JSON.
    /// @param rationaleHash keccak256 hash of the off-chain rationale text.
    /// @param tickersCsv Comma-separated tickers, e.g. "NVDAc,AAPLc,TSLA".
    /// @param weightsBps Target weights in basis points, one per ticker.
    /// @return id 0-based id of the recorded rebalance.
    function recordRebalance(
        bytes32 navSnapshotHash,
        bytes32 rationaleHash,
        string calldata tickersCsv,
        uint256[] calldata weightsBps
    ) external returns (uint256 id) {
        require(msg.sender == owner, "NotOwner");
        require(weightsBps.length >= 1, "NoHoldings");
        require(weightsBps.length <= MAX_HOLDINGS, "TooManyHoldings");
        require(_countTickers(tickersCsv) == weightsBps.length, "TickerWeightMismatch");

        uint256 total = 0;
        bool hasZero = false;
        for (uint256 i = 0; i < weightsBps.length; i++) {
            if (weightsBps[i] == 0) hasZero = true;
            total += weightsBps[i];
        }
        require(!hasZero, "ZeroWeight");
        require(total == TOTAL_BPS, "WeightsNot10000");

        id = rebalanceCount;
        rebalances[id] = Rebalance({
            id: id,
            timestamp: block.timestamp,
            navSnapshotHash: navSnapshotHash,
            rationaleHash: rationaleHash,
            tickersCsv: tickersCsv,
            weightsBps: weightsBps,
            agent: msg.sender
        });
        rebalanceCount++;

        emit RebalanceRecorded(
            id, msg.sender, block.timestamp, navSnapshotHash, rationaleHash, tickersCsv, weightsBps
        );
    }

    /// @notice Fetch a rebalance by id. Reverts if the id does not exist.
    function getRebalance(uint256 id) external view returns (Rebalance memory) {
        require(id < rebalanceCount, "RebalanceNotFound");
        return rebalances[id];
    }

    /// @notice Fetch the most recent rebalance. Reverts if none recorded yet.
    function getLatest() external view returns (Rebalance memory) {
        require(rebalanceCount > 0, "NoRebalances");
        return rebalances[rebalanceCount - 1];
    }

    /// @dev Count tickers in a comma-separated string (commas + 1, 0 if empty).
    function _countTickers(string calldata csv) internal pure returns (uint256) {
        bytes calldata b = bytes(csv);
        if (b.length == 0) return 0;
        uint256 count = 1;
        for (uint256 i = 0; i < b.length; i++) {
            if (b[i] == ",") count++;
        }
        return count;
    }
}
